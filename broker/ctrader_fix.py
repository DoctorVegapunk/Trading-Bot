"""
Pepperstone / cTrader FIX API broker (dual QUOTE + TRADE sessions).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from typing import Any, Optional

import pandas as pd

from broker.bar_store import BarStore
from broker.base import Broker, OpenPosition, Tick
from broker.fix_protocol import parse_md_prices, parse_security_list_raw, utc_timestamp
from broker.fix_session import FixSession
from broker.currency import normalize_currency, to_usd
from broker.history import fetch_hourly_history

log = logging.getLogger(__name__)

BOT_DESIGNATION = "XGB_Bot"
UNITS_PER_LOT = 100_000


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


class CTraderFixBroker(Broker):
    """cTrader FIX 4.4 implementation for headless/server trading."""

    def __init__(self, symbols: list[str], cache_dir: str, open_api_client=None):
        self.symbols = [s.upper() for s in symbols]
        self._cache_dir = cache_dir

        self.host = os.environ.get("FIX_HOST", "demo-us-eqx-01.p.c-trader.com")
        self.quote_port = int(os.environ.get("FIX_QUOTE_PORT", "5211"))
        self.trade_port = int(os.environ.get("FIX_TRADE_PORT", "5212"))
        self.use_ssl = _env_bool("FIX_USE_SSL", True)

        self.sender_comp_id = os.environ.get("FIX_SENDER_COMP_ID", "demo.pepperstone.5294375")
        self.target_comp_id = os.environ.get("FIX_TARGET_COMP_ID", "cServer")
        self.account_login = os.environ.get("FIX_ACCOUNT", "5294375")
        self.password = os.environ.get("FIX_PASSWORD", "")
        self.sender_sub_id = os.environ.get("FIX_SENDER_SUB_ID", self.account_login)

        # Position sizing is always in USD (models assume USD account / USD-quoted pairs).
        self._account_currency = normalize_currency(
            os.environ.get("FIX_ACCOUNT_CURRENCY", "KES")
        )
        balance_usd = os.environ.get("FIX_BALANCE_USD", "").strip()
        if balance_usd:
            self._balance_usd = float(balance_usd)
        else:
            raw = os.environ.get("FIX_BALANCE", "").strip()
            if raw:
                raw_amount = float(raw)
                self._balance_usd = to_usd(raw_amount, self._account_currency)
                log.info(
                    "Converted account equity %.2f %s → %.2f USD for position sizing",
                    raw_amount, self._account_currency, self._balance_usd,
                )
            else:
                self._balance_usd = 150.0
                log.warning(
                    "FIX_BALANCE_USD not set — using default %.2f USD for position sizing. "
                    "Set FIX_BALANCE_USD (recommended) or FIX_BALANCE + FIX_ACCOUNT_CURRENCY.",
                    self._balance_usd,
                )

        self._symbol_ids: dict[str, int] = {}
        self._id_to_symbol: dict[int, str] = {}
        self._load_symbol_overrides()

        self._bars = BarStore(cache_dir)
        self._positions: list[OpenPosition] = []
        self._pos_lock = threading.Lock()
        self._connected = False
        self._lock = threading.Lock()

        self._quote: Optional[FixSession] = None
        self._trade: Optional[FixSession] = None
        self._md_req_ids: dict[str, str] = {}
        self._open_api = open_api_client
        if self._open_api:
            log.info("Open API client provided — live balance and deal history available")

    def _load_symbol_overrides(self) -> None:
        for sym in self.symbols:
            env_key = f"FIX_SYMBOL_{sym}"
            val = os.environ.get(env_key, "")
            if val.isdigit():
                self._symbol_ids[sym] = int(val)

    @property
    def name(self) -> str:
        return f"cTrader FIX ({self.sender_comp_id})"

    def connect(self) -> bool:
        if not self.password:
            log.error("FIX_PASSWORD is not set. Add it to your .env file.")
            return False

        self._quote = FixSession(
            name="QUOTE",
            host=self.host,
            port=self.quote_port,
            use_ssl=self.use_ssl,
            sender_comp_id=self.sender_comp_id,
            target_comp_id=self.target_comp_id,
            target_sub_id="QUOTE",
            sender_sub_id=self.sender_sub_id,
            username=self.account_login,
            password=self.password,
            on_message=self._on_quote_message,
        )
        self._trade = FixSession(
            name="TRADE",
            host=self.host,
            port=self.trade_port,
            use_ssl=self.use_ssl,
            sender_comp_id=self.sender_comp_id,
            target_comp_id=self.target_comp_id,
            target_sub_id="TRADE",
            sender_sub_id=self.sender_sub_id,
            username=self.account_login,
            password=self.password,
            on_message=self._on_trade_message,
        )

        if not self._trade.connect_and_logon():
            return False
        if not self._quote.connect_and_logon():
            self._trade.disconnect()
            return False

        self._request_security_list()
        time.sleep(2.0)
        self._apply_default_symbol_ids()
        self._warmup_bars()
        self._subscribe_all_quotes()
        self._request_positions()

        self._connected = True
        log.info(
            "FIX connected | Account %s | Equity for sizing: %.2f USD",
            self.account_login,
            self._balance_usd,
        )
        return True

    def _apply_default_symbol_ids(self) -> None:
        defaults = {"EURUSD": 1, "GBPUSD": 2}
        for sym in self.symbols:
            if sym not in self._symbol_ids and sym in defaults:
                self._symbol_ids[sym] = defaults[sym]
                log.info("Using default FIX symbol id %s=%s", sym, defaults[sym])
        for sym, sid in self._symbol_ids.items():
            self._id_to_symbol[sid] = sym

    def shutdown(self) -> None:
        self._connected = False
        for sym in self.symbols:
            self._bars.save_cache(sym)
        if self._quote:
            self._quote.disconnect()
        if self._trade:
            self._trade.disconnect()
        if self._open_api:
            self._open_api.disconnect()

    def ensure_connected(self) -> bool:
        if not _check_internet():
            return False
        if (
            self._connected
            and self._quote
            and self._trade
            and self._quote.logged_on
            and self._trade.logged_on
        ):
            return True
        log.warning("FIX session lost — reconnecting...")
        self.shutdown()
        time.sleep(3)
        return self.connect()

    def get_bid_ask_candles(self, symbol: str, count: int) -> Optional[pd.DataFrame]:
        sym = symbol.upper()
        pip = 0.0001 if "JPY" not in sym else 0.01
        df = self._bars.get_dataframe(sym, count, pip_size=pip)
        if df is None or len(df) < 50:
            hist = fetch_hourly_history(sym, count)
            if hist is not None:
                self._bars.seed_history(sym, hist)
                df = self._bars.get_dataframe(sym, count, pip_size=pip)
        return df

    def get_tick(self, symbol: str) -> Optional[Tick]:
        sym = symbol.upper()
        bid, ask = self._bars.get_last_prices(sym)
        if bid and ask:
            return Tick(bid=bid, ask=ask)
        return None

    def get_account_balance(self) -> float:
        """Equity in USD (used for lot sizing). Prefers Open API live balance."""
        if self._open_api and self._open_api.connected:
            try:
                balance, _ = self._open_api.get_balance()
                self._balance_usd = balance
                log.debug("Open API live balance: %.2f USD", balance)
            except Exception as exc:
                log.warning("Open API balance failed, using cached: %s", exc)
        return self._balance_usd

    def get_account_currency(self) -> str:
        return "USD"

    def get_recent_deals(self, hours: int = 24, max_rows: int = 10) -> list[dict]:
        """Fetch recent trade history from Open API. Returns empty list if unavailable."""
        if self._open_api and self._open_api.connected:
            try:
                return self._open_api.get_recent_deals(hours=hours, max_rows=max_rows)
            except Exception as exc:
                log.debug("Open API deal history failed: %s", exc)
        return []

    def refresh_positions(self) -> None:
        self._request_positions()
        time.sleep(0.8)

    def get_bot_positions(self, magic: int = 20250516) -> list[OpenPosition]:
        with self._pos_lock:
            return list(self._positions)

    def place_order(
        self,
        symbol: str,
        direction: str,
        lot_size: float,
        sl_price: float,
        tp_price: float,
        comment: str = BOT_DESIGNATION,
        dry_run: bool = False,
    ) -> bool:
        sym = symbol.upper()
        tick = self.get_tick(sym)
        if tick is None:
            log.error("No live quote for %s", sym)
            return False

        price = tick.ask if direction == "BUY" else tick.bid
        digits = 3 if "JPY" in sym else 5
        sl_price = round(sl_price, digits)
        tp_price = round(tp_price, digits)

        symbol_id = self._symbol_ids.get(sym)
        if symbol_id is None:
            log.error("Unknown FIX symbol id for %s — set FIX_SYMBOL_%s in .env", sym, sym)
            return False

        lot_size = max(round(lot_size, 2), 0.01)
        order_qty = int(round(lot_size * UNITS_PER_LOT))
        if order_qty < 1000:
            order_qty = 1000

        if dry_run:
            log.info(
                "[DRY RUN] Would place %s %.2f lots %s @ ~%.5f | SL=%.5f | TP=%.5f",
                direction, lot_size, sym, price, sl_price, tp_price,
            )
            return True

        if not self._trade or not self._trade.logged_on:
            log.error("TRADE session not connected")
            return False

        cl_ord_id = f"xgb{uuid.uuid4().hex[:12]}"
        side = 1 if direction == "BUY" else 2
        fields: list[tuple[int, Any]] = [
            (11, cl_ord_id),
            (55, symbol_id),
            (54, side),
            (60, utc_timestamp()),
            (38, order_qty),
            (40, 1),   # Market
            (59, 3),   # IOC (ignored by server; market uses IOC)
            (1002, sl_price),
            (1000, tp_price),
            (494, comment[:50]),
        ]
        ok = self._trade.send_application("D", fields)
        if ok:
            log.info(
                "FIX order sent: %s %.2f lots %s (qty=%d) @ ~%.5f | SL=%.5f | TP=%.5f | id=%s",
                direction, lot_size, sym, order_qty, price, sl_price, tp_price, cl_ord_id,
            )
        return ok

    def _warmup_bars(self) -> None:
        for sym in self.symbols:
            self._bars.load_cache(sym)
            hist = fetch_hourly_history(sym, 600)
            if hist is not None:
                self._bars.seed_history(sym, hist)
            self._bars.save_cache(sym)

    def _request_security_list(self) -> None:
        if not self._trade:
            return
        req_id = f"sec{uuid.uuid4().hex[:8]}"
        self._trade.send_application("x", [
            (320, req_id),
            (559, 0),
        ])

    def _subscribe_all_quotes(self) -> None:
        if not self._quote:
            return
        for sym in self.symbols:
            sid = self._symbol_ids.get(sym)
            if sid is None:
                log.warning("Skipping quote subscribe for %s (no symbol id)", sym)
                continue
            md_id = f"md{sym}{uuid.uuid4().hex[:6]}"
            self._md_req_ids[sym] = md_id
            self._quote.send_application("V", [
                (262, md_id),
                (263, 1),
                (264, 1),
                (265, 1),
                (267, 2),
                (269, 0),
                (269, 1),
                (146, 1),
                (55, sid),
            ])
            log.info("Subscribed to %s market data (id=%s)", sym, sid)

    def _request_positions(self) -> None:
        if not self._trade:
            return
        req_id = f"pos{uuid.uuid4().hex[:8]}"
        self._trade.send_application("AN", [(710, req_id)])

    def _on_quote_message(self, tags: dict[str, str], raw: str) -> None:
        msg_type = tags.get("35", "")
        if msg_type not in ("W", "X"):
            return
        sym_id = tags.get("55")
        if not sym_id:
            return
        try:
            sid = int(sym_id)
        except ValueError:
            return
        sym = self._id_to_symbol.get(sid)
        if not sym:
            return
        bid, ask = parse_md_prices(tags, raw)
        if bid and ask:
            self._bars.update_tick(sym, bid, ask)

    def _on_trade_message(self, tags: dict[str, str], raw: str) -> None:
        msg_type = tags.get("35", "")
        if msg_type == "y":
            mapping = parse_security_list_raw(raw)
            for name, sid in mapping.items():
                clean = name.upper().replace("/", "")
                self._symbol_ids[clean] = sid
                self._id_to_symbol[sid] = clean
            if mapping:
                log.info("Security list: %d symbols loaded", len(mapping))
        elif msg_type == "8":
            self._handle_execution_report(tags)
        elif msg_type in ("AP", "AO"):
            self._handle_position_report(tags)
        elif msg_type == "j":
            log.error("FIX business reject: %s", tags.get("58", tags))

    def _handle_execution_report(self, tags: dict[str, str]) -> None:
        exec_type = tags.get("150", "")
        status = tags.get("39", "")
        if exec_type == "F" or status == "2":
            last_qty = tags.get("32", "0")
            avg_px = tags.get("6", tags.get("44", "0"))
            log.info("FIX fill: qty=%s avg_px=%s", last_qty, avg_px)

    def _handle_position_report(self, tags: dict[str, str]) -> None:
        result = tags.get("728", "")
        if result == "2":
            with self._pos_lock:
                self._positions = []
            return
        if result and result != "0":
            return
        sym_id = tags.get("55")
        sym = None
        if sym_id and sym_id.isdigit():
            sym = self._id_to_symbol.get(int(sym_id))
        if not sym and sym_id:
            sym = str(sym_id)
        long_qty = float(tags.get("704", 0) or 0)
        short_qty = float(tags.get("705", 0) or 0)
        pos_id = tags.get("721", "")
        with self._pos_lock:
            self._positions = [p for p in self._positions if p.ticket != pos_id]
            if long_qty > 0:
                self._positions.append(OpenPosition(
                    symbol=sym or "?",
                    direction="BUY",
                    volume=long_qty / UNITS_PER_LOT,
                    ticket=pos_id,
                ))
            if short_qty > 0:
                self._positions.append(OpenPosition(
                    symbol=sym or "?",
                    direction="SELL",
                    volume=short_qty / UNITS_PER_LOT,
                    ticket=pos_id,
                ))


def _check_internet(host: str = "8.8.8.8", port: int = 53, timeout: int = 5) -> bool:
    try:
        socket.setdefaulttimeout(timeout)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((host, port))
        return True
    except OSError:
        return False
