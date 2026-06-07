"""
Firestore logging for multi-bot projects.

Structure (project: my-bots-7bd9a):
    bots/{bot_id}/account/{account_id}              ← balance, status, summary
    bots/{bot_id}/trades/{trade_id}                 ← individual trade records
    bots/{bot_id}/daily_logs/{YYYY-MM-DD}           ← per-day summary (UTC)
    bots/{bot_id}/daily_logs/{YYYY-MM-DD}/entries/  ← log lines for that day

Uses Firebase Admin SDK (service account JSON), not the web client config.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

_app_initialized = False
_init_lock = threading.Lock()


def _row_to_firestore(row: dict) -> dict[str, Any]:
    """Map Excel-style trade row keys to Firestore-friendly fields."""
    return {
        "timestamp_utc": row.get("Timestamp (UTC)"),
        "symbol": row.get("Symbol"),
        "direction": row.get("Direction"),
        "lot_size": row.get("Lot Size"),
        "entry_price": row.get("Entry Price"),
        "sl_price": row.get("SL Price"),
        "tp_price": row.get("TP Price"),
        "stop_pips": row.get("Stop (pips)"),
        "tp_pips": row.get("TP (pips)"),
        "atr": row.get("ATR"),
        "confidence": row.get("Confidence"),
        "buy_prob": row.get("Buy Prob"),
        "sell_prob": row.get("Sell Prob"),
        "adx": row.get("ADX"),
        "atr_regime": row.get("ATR Regime"),
        "ema_200_dist": row.get("EMA 200 Dist"),
        "account_equity": row.get("Account Equity"),
        "risk_amount": row.get("Risk Amount"),
        "currency": row.get("Currency"),
        "dry_run": row.get("Dry Run") == "Yes",
        "signal_reason": row.get("Signal Reason"),
    }


class FirestoreLogger:
    """Always-on Firestore sync for account state, trades, and daily logs."""

    def __init__(self) -> None:
        self.bot_id = os.environ.get("FIREBASE_BOT_ID", "forex-xgb")
        self.account_id = os.environ.get(
            "FIREBASE_ACCOUNT_ID",
            os.environ.get("FIX_ACCOUNT", "default"),
        )
        self._db = None
        self._last_log_day: Optional[str] = None

        try:
            self._db = self._init_firestore()
            log.info(
                "Firestore enabled | project bots/%s account/%s",
                self.bot_id,
                self.account_id,
            )
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

    def _bot_ref(self):
        return self._db.collection("bots").document(self.bot_id)

    def log_trade(self, row_data: dict) -> None:
        if not self._db:
            return
        try:
            from firebase_admin import firestore as fs

            payload = _row_to_firestore(row_data)
            payload["logged_at"] = fs.SERVER_TIMESTAMP
            payload["bot_id"] = self.bot_id
            payload["account_id"] = self.account_id

            trade_ref = self._bot_ref().collection("trades").document()
            trade_ref.set(payload)

            # Bump trade count on account doc
            acct_ref = self._bot_ref().collection("account").document(self.account_id)
            acct_ref.set(
                {
                    "last_trade_at": fs.SERVER_TIMESTAMP,
                    "last_trade_symbol": payload.get("symbol"),
                    "last_trade_direction": payload.get("direction"),
                },
                merge=True,
            )
            log.info("[Firestore] Trade logged → bots/%s/trades/%s", self.bot_id, trade_ref.id)
        except Exception as exc:
            log.error("[Firestore] log_trade failed: %s", exc)

    def update_account(self, summary: dict[str, Any]) -> None:
        """Upsert account-level fields (balance, status, open positions, etc.)."""
        if not self._db:
            return
        try:
            from firebase_admin import firestore as fs

            payload = dict(summary)
            payload["updated_at"] = fs.SERVER_TIMESTAMP
            payload["bot_id"] = self.bot_id
            payload["account_id"] = self.account_id

            self._bot_ref().collection("account").document(self.account_id).set(
                payload,
                merge=True,
            )
        except Exception as exc:
            log.error("[Firestore] update_account failed: %s", exc)

    def _daily_log_ref(self, day: str):
        """Document for one UTC calendar day."""
        return self._bot_ref().collection("daily_logs").document(day)

    def append_daily_log_entries(self, entries: list[dict[str, Any]]) -> None:
        """Write batched log lines under today's UTC date."""
        if not self._db or not entries:
            return
        try:
            from firebase_admin import firestore as fs

            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            day_ref = self._daily_log_ref(day)

            batch = self._db.batch()
            level_counts: dict[str, int] = {}
            for entry in entries:
                payload = dict(entry)
                payload["account_id"] = self.account_id
                payload["bot_id"] = self.bot_id
                payload["logged_at"] = fs.SERVER_TIMESTAMP
                ref = day_ref.collection("entries").document()
                batch.set(ref, payload)
                level_counts[entry.get("level", "INFO")] = (
                    level_counts.get(entry.get("level", "INFO"), 0) + 1
                )

            summary: dict[str, Any] = {
                "date": day,
                "account_id": self.account_id,
                "bot_id": self.bot_id,
                "last_entry_at": fs.SERVER_TIMESTAMP,
                "entry_count": fs.Increment(len(entries)),
            }
            for lvl, count in level_counts.items():
                key = f"count_{lvl.lower()}"
                summary[key] = fs.Increment(count)

            if self._last_log_day != day:
                summary["started_at"] = fs.SERVER_TIMESTAMP
                self._last_log_day = day

            batch.set(day_ref, summary, merge=True)
            batch.commit()
        except Exception as exc:
            log.error("[Firestore] append_daily_log_entries failed: %s", exc)


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
        "currency": currency,
        "open_positions": open_positions,
        "instruments": instruments,
        "dry_run": dry_run,
        "status": "offline" if offline else "running",
        "heartbeat_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
