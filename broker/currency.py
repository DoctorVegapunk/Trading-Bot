"""Convert account equity to USD for position sizing."""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Common aliases → ISO 4217
_CURRENCY_ALIASES = {
    "KSH": "KES",
    "SHILLING": "KES",
    "KENYAN SHILLING": "KES",
}


def normalize_currency(code: str) -> str:
    c = (code or "USD").strip().upper()
    return _CURRENCY_ALIASES.get(c, c)


def to_usd(amount: float, currency: str) -> float:
    """
    Convert `amount` in `currency` to USD.
    Uses Yahoo Finance FX tickers (cached per process).
    """
    ccy = normalize_currency(currency)
    if ccy == "USD":
        return amount
    if amount <= 0:
        return 0.0

    rate = _fetch_usd_per_unit(ccy)
    if rate is None:
        log.warning(
            "Cannot convert %s to USD (no FX rate). Using raw amount %.2f as USD — "
            "set FIX_BALANCE_USD in .env to avoid wrong lot sizes.",
            ccy, amount,
        )
        return amount
    # rate = USD per 1 unit of foreign currency (e.g. 1 KES = 0.0077 USD)
    return amount * rate


_fx_cache: dict[str, float] = {}


def _fetch_usd_per_unit(currency: str) -> Optional[float]:
    if currency in _fx_cache:
        return _fx_cache[currency]

    pair = f"{currency}USD=X"  # e.g. KESUSD=X
    try:
        import yfinance as yf
        ticker = yf.Ticker(pair)
        usd_per_unit: Optional[float] = None
        try:
            price = ticker.fast_info.get("last_price")
            if price and price > 0:
                usd_per_unit = float(price)
        except Exception:
            pass
        if usd_per_unit is None:
            hist = ticker.history(period="5d")
            if not hist.empty:
                usd_per_unit = float(hist["Close"].iloc[-1])
        if usd_per_unit is None:
            inv = yf.Ticker(f"USD{currency}=X")
            inv_hist = inv.history(period="5d")
            if inv_hist.empty:
                return None
            usd_per_unit = 1.0 / float(inv_hist["Close"].iloc[-1])
        _fx_cache[currency] = usd_per_unit
        return usd_per_unit
    except Exception as exc:
        log.debug("FX lookup failed for %s: %s", currency, exc)
        return None
