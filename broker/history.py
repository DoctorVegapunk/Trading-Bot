"""
Historical 1H forex candles for indicator warm-up (used when FIX has no bar API).
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# Yahoo Finance ticker suffix for major pairs
_YAHOO_MAP = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCHF": "USDCHF=X",
    "USDCAD": "USDCAD=X",
    "NZDUSD": "NZDUSD=X",
}


def fetch_hourly_history(symbol: str, bars: int = 600) -> Optional[pd.DataFrame]:
    """
    Download ~`bars` closed 1H candles via Yahoo Finance.
    Used only for feature warm-up; live prices come from FIX.
    """
    yahoo = _YAHOO_MAP.get(symbol.upper())
    if not yahoo:
        yahoo = f"{symbol.upper()}=X"

    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed — pip install yfinance (required for FIX mode)")
        return None

    # Request enough calendar days to cover `bars` trading hours
    period = "730d" if bars > 500 else "365d"
    try:
        ticker = yf.Ticker(yahoo)
        hist = ticker.history(interval="1h", period=period, auto_adjust=False)
    except Exception as exc:
        log.error("Yahoo history failed for %s: %s", symbol, exc)
        return None

    if hist is None or hist.empty:
        log.error("No Yahoo history for %s (%s)", symbol, yahoo)
        return None

    hist = hist.rename(columns={
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    })
    if hist.index.tz is None:
        hist.index = hist.index.tz_localize("UTC")
    else:
        hist.index = hist.index.tz_convert("UTC")

    df = hist[["open", "high", "low", "close", "volume"]].copy()
    # Yahoo FX hourly volume is usually 0 → breaks vol_ratio in add_features.
    df["volume"] = df["volume"].fillna(0).clip(lower=1.0)
    df.loc[df["volume"] <= 0, "volume"] = 1.0

    # Align to clean 1H UTC bars (avoids duplicate/misaligned timestamps vs FIX ticks)
    df = df.resample("1h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["close"])
    df["volume"] = df["volume"].clip(lower=1.0)

    spread_est = 0.0001
    half = spread_est / 2.0
    df["ask_open"] = df["open"] + half
    df["ask_high"] = df["high"] + half
    df["ask_low"] = df["low"] + half
    df["ask_close"] = df["close"] + half
    df["spread_pips"] = spread_est / 0.0001

    # Need ~200+ bars warm-up for EMA-200 / roc_120; keep extra after resample.
    keep = max(bars, 650)
    if len(df) > keep:
        df = df.iloc[-keep:]
    log.info("Loaded %d H1 bars from Yahoo for %s", len(df), symbol)
    return df
