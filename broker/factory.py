"""Broker factory and module-level singleton (FIX + optional Open API)."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from broker.base import Broker
from broker.ctrader_fix import CTraderFixBroker

log = logging.getLogger(__name__)

_broker: Optional[Broker] = None


def create_broker(
    symbols: list[str],
    cache_dir: str,
    open_api_client: Any = None,
) -> Broker:
    return CTraderFixBroker(
        symbols=symbols, cache_dir=cache_dir, open_api_client=open_api_client,
    )


def get_broker() -> Broker:
    if _broker is None:
        raise RuntimeError("Broker not initialized — call init_broker() first")
    return _broker


def _try_init_open_api() -> Any:
    """Try to create and connect an Open API client from env vars. Returns None if missing."""
    account_id = os.environ.get("OPEN_API_CTID_TRADER_ACCOUNT_ID", "").strip()
    client_id = os.environ.get("OPEN_API_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OPEN_API_CLIENT_SECRET", "").strip()
    if not all([account_id, client_id, client_secret]):
        return None
    from broker.open_api_client import OpenApiClient

    client = OpenApiClient()
    try:
        fix_host = os.environ.get("FIX_HOST", "demo-")
        is_live = "demo" not in fix_host.lower()
        ok = client.connect(
            ctid_trader_account_id=int(account_id),
            client_id=client_id,
            client_secret=client_secret,
            is_live=is_live,
            timeout=30.0,
        )
        if ok:
            log.info("Open API connected — live balance and deal history enabled")
            return client
        log.warning("Open API connect returned False — check credentials")
    except Exception as exc:
        log.warning("Open API init skipped: %s", exc)
    return None


def init_broker(symbols: list[str], cache_dir: str) -> Broker:
    global _broker
    open_api = _try_init_open_api()
    _broker = create_broker(symbols, cache_dir, open_api_client=open_api)
    return _broker


def load_dotenv(path: str) -> None:
    """Minimal .env loader (no external dependency)."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
