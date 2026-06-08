"""
Firestore logging for XGBoost Trading Bot.

Structure:
    trades/{YYYY-MM-DD}/hours/{HH}/instruments/{SYMBOL}   ← hourly signal entry
    account/{account_id}                                     ← account summary
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

_KES_RATE: float | None = None


def _get_kes_rate() -> float | None:
    """Fetch or return cached KES->USD rate."""
    global _KES_RATE
    if _KES_RATE is not None:
        return _KES_RATE
    from broker.currency import _fetch_usd_per_unit
    try:
        _KES_RATE = _fetch_usd_per_unit("KES")
    except Exception:
        pass
    return _KES_RATE


def _usd_to_kes(usd: float) -> float | None:
    rate = _get_kes_rate()
    if rate is None or rate <= 0:
        return None
    return round(usd / rate, 2)

_app_initialized = False
_init_lock = threading.Lock()


class FirestoreLogger:
    def __init__(self) -> None:
        self.account_id = os.environ.get(
            "FIREBASE_ACCOUNT_ID",
            os.environ.get("FIX_ACCOUNT", "default"),
        )
        self._db = None

        try:
            self._db = self._init_firestore()
            log.info("Firestore enabled | account/%s", self.account_id)
        except Exception as exc:
            log.critical("Firestore init failed: %s", exc)
            raise

    @staticmethod
    def _init_firestore():
        global _app_initialized
        import firebase_admin
        from firebase_admin import credentials, firestore

        with _init_lock:
            if not _app_initialized:
                cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
                if cred_path and os.path.isfile(cred_path):
                    cred = credentials.Certificate(cred_path)
                    firebase_admin.initialize_app(cred)
                else:
                    firebase_admin.initialize_app()
                _app_initialized = True
        return firestore.client()

    def log_hour(
        self,
        symbol: str,
        signal: dict,
        *,
        traded: bool = False,
        lot_size: float | None = None,
        entry_price: float | None = None,
        sl_price: float | None = None,
        tp_price: float | None = None,
        account_equity: float | None = None,
        dry_run: bool = False,
    ) -> None:
        if not self._db:
            return
        try:
            now = datetime.now(timezone.utc)
            day = now.strftime("%Y-%m-%d")
            hour = now.strftime("%H")

            doc_path = (
                self._db.collection("trades")
                .document(day)
                .collection("hours")
                .document(hour)
                .collection("instruments")
                .document(symbol)
            )

            payload = {
                "traded": traded,
                "symbol": symbol,
                "direction": signal.get("direction") if traded else None,
                "confidence": round(signal.get("confidence", 0.0), 4),
                "buy_prob": round(signal.get("buy_prob", 0.0), 4),
                "sell_prob": round(signal.get("sell_prob", 0.0), 4),
                "lot_size": lot_size,
                "entry_price": entry_price,
                "sl_price": sl_price,
                "tp_price": tp_price,
                "atr": round(signal.get("atr", 0.0), 6),
                "adx": round(signal.get("adx", 0.0), 2),
                "signal_reason": signal.get("regime_reason", "All filters passed"),
                "account_equity": round(account_equity, 2) if account_equity else None,
                "account_equity_kes": _usd_to_kes(account_equity) if account_equity else None,
                "dry_run": dry_run,
                "timestamp_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
            }

            doc_path.set(payload)
        except Exception as exc:
            log.error("[Firestore] log_hour failed: %s", exc)

    def update_account(self, summary: dict[str, Any]) -> None:
        if not self._db:
            return
        try:
            from firebase_admin import firestore as fs

            payload = dict(summary)
            payload["updated_at"] = fs.SERVER_TIMESTAMP
            payload["account_id"] = self.account_id

            self._db.collection("account").document(self.account_id).set(
                payload, merge=True
            )
        except Exception as exc:
            log.error("[Firestore] update_account failed: %s", exc)


def build_account_summary(
    broker_name: str,
    broker_backend: str,
    balance: float,
    currency: str,
    open_positions: int,
    instruments: list[str],
    dry_run: bool,
    offline: bool = False,
) -> dict[str, Any]:
    return {
        "broker": broker_name,
        "broker_backend": broker_backend,
        "balance_usd": round(balance, 2),
        "balance_kes": _usd_to_kes(balance),
        "currency": currency,
        "open_positions": open_positions,
        "instruments": instruments,
        "dry_run": dry_run,
        "status": "offline" if offline else "running",
        "heartbeat_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
