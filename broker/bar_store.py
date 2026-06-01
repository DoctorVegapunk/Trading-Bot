"""
Build and cache 1H OHLC bars from bid/ask mid prices (FIX tick stream).
"""

from __future__ import annotations

import os
import pickle
import threading
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from broker.ohlc_utils import normalize_ohlc_df


class BarStore:
    """Thread-safe 1H candle store per symbol."""

    def __init__(self, cache_dir: str, timeframe_seconds: int = 3600):
        self.cache_dir = cache_dir
        self.timeframe_seconds = timeframe_seconds
        self._lock = threading.Lock()
        self._bars: dict[str, pd.DataFrame] = {}
        self._forming: dict[str, dict] = {}
        self._last_bid: dict[str, float] = {}
        self._last_ask: dict[str, float] = {}
        os.makedirs(cache_dir, exist_ok=True)

    def load_cache(self, symbol: str) -> None:
        path = self._cache_path(symbol)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                df = pickle.load(fh)
            if isinstance(df, pd.DataFrame) and len(df) > 0:
                self._bars[symbol] = normalize_ohlc_df(df.sort_index())

    def save_cache(self, symbol: str) -> None:
        with self._lock:
            df = self._bars.get(symbol)
        if df is None or df.empty:
            return
        with open(self._cache_path(symbol), "wb") as fh:
            pickle.dump(df, fh)

    def seed_history(self, symbol: str, df: pd.DataFrame, pip_size: float = 0.0001) -> None:
        """Merge historical OHLC (e.g. from Yahoo) into the store."""
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(df.columns):
            raise ValueError(f"History missing columns: {required - set(df.columns)}")
        df = normalize_ohlc_df(df, pip_size)
        with self._lock:
            existing = self._bars.get(symbol)
            if existing is not None and len(existing) > 0:
                merged = pd.concat([df, existing])
                merged = merged[~merged.index.duplicated(keep="last")].sort_index()
                self._bars[symbol] = merged
            else:
                self._bars[symbol] = df.sort_index()

    def update_tick(self, symbol: str, bid: float, ask: float, ts: Optional[datetime] = None) -> None:
        if bid <= 0 or ask <= 0:
            return
        ts = ts or datetime.now(timezone.utc)
        mid = (bid + ask) / 2.0
        spread = ask - bid
        bar_open = ts.replace(minute=0, second=0, microsecond=0)

        with self._lock:
            self._last_bid[symbol] = bid
            self._last_ask[symbol] = ask
            forming = self._forming.get(symbol)
            if forming is None or forming["time"] != bar_open:
                if forming is not None:
                    self._append_closed_bar(symbol, forming)
                forming = {
                    "time": bar_open,
                    "open": mid,
                    "high": mid,
                    "low": mid,
                    "close": mid,
                    "volume": 1.0,
                    "spread_sum": spread,
                    "spread_n": 1,
                }
                self._forming[symbol] = forming
            else:
                forming["high"] = max(forming["high"], mid)
                forming["low"] = min(forming["low"], mid)
                forming["close"] = mid
                forming["volume"] += 1.0
                forming["spread_sum"] += spread
                forming["spread_n"] += 1

    def _append_closed_bar(self, symbol: str, forming: dict) -> None:
        avg_spread = forming["spread_sum"] / max(forming["spread_n"], 1)
        half = avg_spread / 2.0
        row = {
            "open": forming["open"],
            "high": forming["high"],
            "low": forming["low"],
            "close": forming["close"],
            "volume": forming["volume"],
            "ask_open": forming["open"] + half,
            "ask_high": forming["high"] + half,
            "ask_low": forming["low"] + half,
            "ask_close": forming["close"] + half,
            "spread_pips": avg_spread / 0.0001,
        }
        idx = forming["time"]
        if idx.tzinfo is None:
            idx = idx.replace(tzinfo=timezone.utc)
        df_row = pd.DataFrame([row], index=[idx])
        existing = self._bars.get(symbol)
        if existing is None:
            self._bars[symbol] = df_row
        else:
            self._bars[symbol] = pd.concat([existing, df_row])
            self._bars[symbol] = self._bars[symbol][~self._bars[symbol].index.duplicated(keep="last")]

    def get_last_prices(self, symbol: str) -> tuple[Optional[float], Optional[float]]:
        with self._lock:
            return self._last_bid.get(symbol), self._last_ask.get(symbol)

    def get_dataframe(self, symbol: str, count: int, pip_size: float = 0.0001) -> Optional[pd.DataFrame]:
        with self._lock:
            df = self._bars.get(symbol)
            forming = self._forming.get(symbol)
            bid = self._last_bid.get(symbol)
            ask = self._last_ask.get(symbol)

        if df is None or len(df) < 50:
            return None

        out = df.copy()
        # Include current forming bar as latest row (matches MT5 copy_rates behaviour)
        if forming is not None:
            half = ((ask - bid) / 2.0) if bid and ask else pip_size * 0.5
            spread_pips = (ask - bid) / pip_size if bid and ask else 1.0
            fidx = forming["time"]
            if fidx.tzinfo is None:
                fidx = fidx.replace(tzinfo=timezone.utc)
            out.loc[fidx] = {
                "open": forming["open"],
                "high": forming["high"],
                "low": forming["low"],
                "close": forming["close"],
                "volume": forming["volume"],
                "ask_open": forming["open"] + half,
                "ask_high": forming["high"] + half,
                "ask_low": forming["low"] + half,
                "ask_close": forming["close"] + half,
                "spread_pips": spread_pips,
            }
            out = out.sort_index()

        if len(out) > count:
            out = out.iloc[-count:]
        return normalize_ohlc_df(out, pip_size)

    def _cache_path(self, symbol: str) -> str:
        return os.path.join(self.cache_dir, f"{symbol}_h1.pkl")
