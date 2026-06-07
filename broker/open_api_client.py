"""
Synchronous wrapper around cTrader Open API (async Twisted-based SDK).

Provides live account balance and deal history that the FIX API cannot supply.
Runs the Twisted reactor in a background daemon thread.

Usage:
    client = OpenApiClient()
    client.connect(account_id, access_token, client_id, client_secret, is_live=False)
    balance, currency = client.get_balance()
    deals = client.get_recent_deals(hours=24)
    client.disconnect()
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from broker.open_api_auth import OpenApiAuth

log = logging.getLogger(__name__)


class OpenApiClient:
    def __init__(self):
        self._client = None
        self._reactor = None
        self._reactor_thread: Optional[threading.Thread] = None
        self._connected = False
        self._account_id = 0
        self._asset_cache: dict[int, str] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def connect(
        self,
        ctid_trader_account_id: int,
        client_id: str,
        client_secret: str,
        is_live: bool = False,
        timeout: float = 15.0,
    ) -> bool:
        """Connect and authenticate with the Open API server. Uses OAuth 2.0 for the access token."""
        self._account_id = ctid_trader_account_id

        from twisted.internet import reactor
        self._reactor = reactor

        host = (
            "live.ctraderapi.com"
            if is_live
            else "demo.ctraderapi.com"
        )
        port = 5035

        from ctrader_open_api import (
            Client,
            Protobuf,
            TcpProtocol,
        )
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAAccountAuthReq,
            ProtoOAApplicationAuthReq,
            ProtoOAAssetListReq,
            ProtoOAAssetListRes,
            ProtoOATraderReq,
            ProtoOATraderRes,
        )

        event = threading.Event()
        errors: list[str] = []

        def _setup():
            try:
                self._client = Client(
                    host, port, TcpProtocol,
                    numberOfMessagesToSendPerSecond=50,
                )
                self._client.setMessageReceivedCallback(self._on_message)
                self._client.startService()

                log.debug("Open API connecting to %s:%d", host, port)

                # Chain: connect → app auth → account auth
                # client.send() internally waits for TCP + sends request + waits for response
                app_req = ProtoOAApplicationAuthReq()
                app_req.clientId = client_id
                app_req.clientSecret = client_secret
                d = self._client.send(app_req, responseTimeoutInSeconds=timeout)

                def _on_app_auth(_):
                    try:
                        oauth = OpenApiAuth(client_id, client_secret)
                        oauth_access_token = oauth.get_valid_token()
                    except Exception as e:
                        errors.append(f"OAuth token error: {e}")
                        event.set()
                        return

                    # Step 1: Verify access token via GetAccountListByAccessToken
                    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                        ProtoOAGetAccountListByAccessTokenReq,
                    )
                    list_req = ProtoOAGetAccountListByAccessTokenReq()
                    list_req.accessToken = oauth_access_token
                    d_list = self._client.send(list_req, responseTimeoutInSeconds=timeout)
                    d_list.addErrback(lambda f: (errors.append(f"Account list failed: {f.value}"), event.set()))

                    def _on_account_list(resp):
                        from ctrader_open_api import Protobuf
                        inner = Protobuf.extract(resp)

                        ec = getattr(inner, "errorCode", None)
                        if ec:
                            errors.append(f"Account list error {ec}: {getattr(inner, 'description', '')}")
                            event.set()
                            return

                        # Use the first account from the list (server-authoritative)
                        accounts = getattr(inner, "ctidTraderAccount", [])
                        if not accounts:
                            errors.append("No trading accounts linked to this access token")
                            event.set()
                            return
                        actual_account_id = int(accounts[0].ctidTraderAccountId)
                        # Update with the server-authoritative account ID
                        self._account_id = actual_account_id
                        log.debug("Using ctidTraderAccountId=%s from account list", actual_account_id)

                        # Step 2: Authenticate the specific trading account
                        acct_req = ProtoOAAccountAuthReq()
                        acct_req.ctidTraderAccountId = actual_account_id
                        acct_req.accessToken = oauth_access_token
                        d3 = self._client.send(acct_req, responseTimeoutInSeconds=timeout)
                        d3.addErrback(lambda f: (errors.append(f"Account auth failed: {f.value}"), event.set()))

                        def _on_account_auth(resp2):
                            from ctrader_open_api import Protobuf
                            inner2 = Protobuf.extract(resp2)
                            ec2 = getattr(inner2, "errorCode", None)
                            if ec2:
                                errors.append(f"Account auth error {ec2}: {getattr(inner2, 'description', '')}")
                            else:
                                self._connected = True
                            event.set()

                        d3.addCallback(_on_account_auth)
                        return d3

                    d_list.addCallback(_on_account_list)
                    return d_list

                d.addCallback(_on_app_auth)
                d.addErrback(lambda f: (errors.append(f"App auth failed: {f.value}"), event.set()))

            except Exception as e:
                errors.append(str(e))
                event.set()

        self._reactor.callFromThread(_setup)

        # Start reactor thread
        def _run_reactor():
            try:
                self._reactor.run(installSignalHandlers=False)
            except Exception:
                pass

        self._reactor_thread = threading.Thread(target=_run_reactor, daemon=True, name="oa-reactor")
        self._reactor_thread.start()

        if not event.wait(timeout):
            log.error("Open API connect timed out after %ss", timeout)
            return False

        if errors:
            log.error("Open API connect errors: %s", "; ".join(errors))
            return False

        if not self._connected:
            return False

        # Pre-fetch asset list for currency resolution
        self._load_asset_cache(timeout)
        return True

    def disconnect(self) -> None:
        """Shut down the Open API connection cleanly."""
        self._connected = False
        if self._reactor and self._reactor.running:
            self._reactor.callFromThread(self._reactor.stop)

    @property
    def connected(self) -> bool:
        return self._connected

    def get_balance(self, timeout: float = 15.0) -> tuple[float, str]:
        """
        Fetch live balance from Open API.

        Returns (balance_float, currency_string).
        Raises RuntimeError if the request fails.
        """
        self._require_connected()

        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOATraderReq, ProtoOATraderRes

        req = ProtoOATraderReq()
        req.ctidTraderAccountId = self._account_id
        resp = self._send_and_wait(req, ProtoOATraderRes, timeout)

        trader = resp.trader
        money_digits = trader.moneyDigits or 0
        balance = trader.balance / (10 ** money_digits)
        currency = self._resolve_asset_name(trader.depositAssetId)
        return balance, currency

    def get_recent_deals(
        self,
        hours: int = 24,
        max_rows: int = 100,
        timeout: float = 15.0,
    ) -> list[dict[str, Any]]:
        """
        Fetch recent deals (trade executions) from Open API.

        Args:
            hours: Look back window in hours.
            max_rows: Maximum number of deals to return.
            timeout: Per-request timeout.

        Returns a list of dicts with keys:
            deal_id, order_id, position_id, symbol_id,
            direction, volume, price, commission, gross_profit,
            swap, balance, timestamp_utc
        """
        self._require_connected()

        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOADealListReq,
            ProtoOADealListRes,
        )

        now_ms = int(time.time() * 1000)
        from_ms = now_ms - (hours * 3600 * 1000)

        req = ProtoOADealListReq()
        req.ctidTraderAccountId = self._account_id
        req.fromTimestamp = from_ms
        req.toTimestamp = now_ms
        req.maxRows = max_rows

        resp = self._send_and_wait(req, ProtoOADealListRes, timeout)

        deals = []
        for deal in resp.deal:
            md = deal.moneyDigits or 0
            div = 10 ** md
            entry = {
                "deal_id": deal.dealId,
                "order_id": deal.orderId,
                "position_id": deal.positionId,
                "symbol_id": deal.symbolId,
                "direction": "BUY" if deal.tradeSide == 1 else "SELL",
                "volume": deal.volume / 100.0,
                "price": deal.executionPrice,
                "commission": deal.commission / div if deal.commission else 0.0,
                "gross_profit": 0.0,
                "swap": 0.0,
                "balance": 0.0,
                "timestamp_utc": datetime.fromtimestamp(
                    deal.executionTimestamp / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S"),
            }
            if deal.HasField("closePositionDetail"):
                cpd = deal.closePositionDetail
                entry["gross_profit"] = cpd.grossProfit / div if cpd.grossProfit else 0.0
                entry["swap"] = cpd.swap / div if cpd.swap else 0.0
                entry["commission"] = cpd.commission / div if cpd.commission else 0.0
                entry["balance"] = cpd.balance / div if cpd.balance else 0.0
            deals.append(entry)

        return deals

    # ── Internal ──────────────────────────────────────────────────────────

    def _require_connected(self):
        if not self._connected or not self._client:
            raise RuntimeError("Open API not connected — call connect() first")

    def _on_message(self, client, message):
        """Raw message callback from SDK (called in reactor thread)."""
        pass  # We rely on Deferred matching instead

    def _load_asset_cache(self, timeout: float = 15.0):
        """Fetch asset list to build deposit-asset-id → currency-name mapping."""
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOAAssetListReq,
                ProtoOAAssetListRes,
            )

            req = ProtoOAAssetListReq()
            req.ctidTraderAccountId = self._account_id
            resp = self._send_and_wait(req, ProtoOAAssetListRes, timeout)
            for asset in resp.asset:
                self._asset_cache[asset.assetId] = asset.name
        except Exception as exc:
            log.warning("Failed to load asset cache: %s", exc)

    def _resolve_asset_name(self, asset_id: int) -> str:
        """Map deposit asset ID to a currency string."""
        name = self._asset_cache.get(asset_id, "")
        if not name:
            return "USD"
        # Remove common suffixes
        name = name.upper().replace(" ", "")
        return name

    def _send_and_wait(self, request, expected_type, timeout: float = 30.0):
        """
        Send a ProtoOA request and wait for the response synchronously.

        Args:
            request: Protobuf request message instance.
            expected_type: Expected response protobuf type (used for validation).
            timeout: Max seconds to wait.

        Returns the inner response protobuf message (payload extracted from
        ProtoMessage wrapper).
        Raises RuntimeError on failure, timeout, or API error response.
        """
        from ctrader_open_api import Protobuf

        event = threading.Event()
        result: list[Any] = [None]
        error: list[Optional[str]] = [None]

        def _on_response(resp):
            # resp is a ProtoMessage wrapper — extract the inner payload
            try:
                inner = Protobuf.extract(resp)
            except Exception as e:
                error[0] = f"Failed to parse response: {e}"
                event.set()
                return

            if getattr(inner, "errorCode", None):
                ec = inner.errorCode
                desc = getattr(inner, "description", "")
                error[0] = f"API error {ec}: {desc}"
            else:
                result[0] = inner
            event.set()

        def _on_failure(failure):
            error[0] = str(failure.value) if hasattr(failure, "value") else str(failure)
            event.set()

        def _do_send():
            if not self._client:
                error[0] = "Client not initialized"
                event.set()
                return
            d = self._client.send(request, responseTimeoutInSeconds=timeout)
            d.addCallbacks(_on_response, _on_failure)

        self._reactor.callFromThread(_do_send)

        if not event.wait(timeout + 5):
            raise RuntimeError(f"Request timed out after {timeout}s")

        if error[0]:
            raise RuntimeError(error[0])

        resp = result[0]
        if resp is None:
            raise RuntimeError("No response received")

        return resp
