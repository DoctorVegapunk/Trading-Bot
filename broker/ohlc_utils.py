"""OHLC dataframe normalization shared by live_trader and bar store."""

from __future__ import annotations

import pandas as pd


def normalize_ohlc_df(df: pd.DataFrame, pip_size: float = 0.0001) -> pd.DataFrame:
    """
    Ensure OHLCV + bid/ask/spread columns exist and are usable for feature engineering.
    Yahoo forex feeds often report volume=0, which breaks vol_ratio (0/0 → NaN).
    """
    out = df.sort_index().copy()
    spread_price = pip_size * 10  # 1 pip default
    half = spread_price / 2.0

    if "volume" not in out.columns:
        out["volume"] = 1.0
    out["volume"] = out["volume"].fillna(0)
    out.loc[out["volume"] <= 0, "volume"] = 1.0

    if "spread_pips" not in out.columns:
        out["spread_pips"] = spread_price / pip_size
    else:
        out["spread_pips"] = out["spread_pips"].fillna(spread_price / pip_size)

    for mid, ask in (
        ("open", "ask_open"),
        ("high", "ask_high"),
        ("low", "ask_low"),
        ("close", "ask_close"),
    ):
        if ask not in out.columns:
            out[ask] = out[mid] + half
        else:
            out[ask] = out[ask].fillna(out[mid] + half)

    return out
