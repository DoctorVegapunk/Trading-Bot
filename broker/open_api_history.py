"""
Third-tier fallback: fetch historical 1H candles via cTrader Open API Trendbars.

Used when both Yahoo Finance and FIX BarStore are unavailable.
Requires the Open API client to be connected.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

log = logging.getLogger(__name__)

# Open API period enum values (from ProtoOATrendbarPeriod)
PERIOD_H1 = 9

# Hard-coded symbol ID map for common pairs (Open API matches cTrader defaults)
_DEFAULT_SYMBOL_IDS = {
    "EURUSD": 1,
    "GBPUSD": 2,
    "USDJPY": 3,
    "AUDUSD": 4,
    "USDCHF": 5,
    "USDCAD": 6,
    "NZDUSD": 7,
}


def fetch_open_api_history(
    symbol: str,
    bars: int = 600,
    open_api_client: Any = None,
    broker: Any = None,
) -> Optional[pd.DataFrame]:
    """
    Download ~`bars` closed 1H candles via cTrader Open API Trendbars.

    Args:
        symbol: Instrument name (e.g. "EURUSD").
        bars: Number of candles to request.
        open_api_client: Connected OpenApiClient instance.
        symbol_id_map: Optional dict[str, int] mapping symbol names to
                       Open API symbol IDs. Falls back to hard-coded defaults.

    Returns a DataFrame matching the format of fetch_hourly_history(), or None.
    """
    if open_api_client is None or not getattr(open_api_client, "connected", False):
        log.warning("Open API not connected — cannot fetch historical candles")
        return None

    # Resolve symbol ID (try broker first, then hard-coded defaults)
    symbol_upper = symbol.upper().replace("-", "")
    sym_id = None
    if broker is not None:
        get_id = getattr(broker, "get_symbol_id", None)
        if get_id:
            sym_id = get_id(symbol)
    if sym_id is None:
        sym_id = _DEFAULT_SYMBOL_IDS.get(symbol_upper)
    if sym_id is None:
        log.warning("No Open API symbol ID for %s", symbol_upper)
        return None

    try:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAGetTrendbarsReq,
        )
        from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
            ProtoOATrendbar,
        )

        # We need the ctidTraderAccountId — pull it from the client
        account_id = getattr(open_api_client, "_account_id", 0)
        if not account_id:
            log.warning("Open API client has no account ID")
            return None

        now_utc = int(datetime.now(timezone.utc).timestamp() * 1000)
        from_ts = now_utc - (bars * 3600 * 1000)

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = account_id
        req.symbolId = sym_id
        req.period = PERIOD_H1
        req.fromTimestamp = from_ts
        req.toTimestamp = now_utc

        # Use expected_type to avoid polluting the log with the message name
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAGetTrendbarsRes,
        )
        resp = open_api_client._send_and_wait(req, ProtoOAGetTrendbarsRes, timeout=15.0)

    except Exception as exc:
        log.warning("Open API trendbars failed for %s: %s", symbol, exc)
        return None

    trendbars = getattr(resp, "trendbar", [])
    if not trendbars:
        log.warning("Open API returned no trendbars for %s", symbol)
        return None

    rows = []
    for tb in trendbars:
        low = tb.low / 1e5
        rows.append({
            "timestamp": tb.utcTimestampInMinutes * 60,
            "open": low + (tb.deltaOpen / 1e5),
            "high": low + (tb.deltaHigh / 1e5),
            "low": low,
            "close": low + (tb.deltaClose / 1e5),
            "volume": float(tb.volume),
        })

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.set_index("timestamp").sort_index()

    # Match the standard Yahoo fetch format
    spread_est = 0.0001
    half = spread_est / 2.0
    df["ask_open"] = df["open"] + half
    df["ask_high"] = df["high"] + half
    df["ask_low"] = df["low"] + half
    df["ask_close"] = df["close"] + half
    df["spread_pips"] = spread_est / 0.0001
    df["volume"] = df["volume"].clip(lower=1.0)

    keep = max(bars, 650)
    if len(df) > keep:
        df = df.iloc[-keep:]

    log.info("Loaded %d H1 bars from Open API for %s", len(df), symbol)
    return df
