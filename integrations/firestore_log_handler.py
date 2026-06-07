"""
Python logging.Handler → Firestore daily log buckets (UTC date).

Structure:
    bots/{bot_id}/daily_logs/{YYYY-MM-DD}           ← day summary
    bots/{bot_id}/daily_logs/{YYYY-MM-DD}/entries/  ← individual log lines
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from integrations.firestore_logger import FirestoreLogger


class FirestoreDailyLogHandler(logging.Handler):
    """Buffers log records and flushes to Firestore in batches."""

    def __init__(
        self,
        firestore_logger: "FirestoreLogger",
        *,
        flush_interval_sec: float = 30.0,
        flush_size: int = 20,
        min_level: int = logging.INFO,
    ):
        super().__init__(level=min_level)
        self._fs = firestore_logger
        self._flush_interval = flush_interval_sec
        self._flush_size = flush_size
        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._flush_loop, daemon=True, name="fs-log-flush")
        self._thread.start()
        atexit.register(self.close)

    def emit(self, record: logging.LogRecord) -> None:
        # Avoid feedback loop from Firestore integration loggers
        if record.name.startswith("integrations.firestore"):
            return
        try:
            msg = self.format(record)
            entry = {
                "level": record.levelname,
                "message": msg[:4000],  # Firestore doc size guard
                "timestamp_utc": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "logger": record.name,
            }
            with self._lock:
                self._buffer.append(entry)
                if len(self._buffer) >= self._flush_size:
                    self._flush_locked()
        except Exception:
            self.handleError(record)

    def _flush_loop(self) -> None:
        while not self._stop.wait(self._flush_interval):
            with self._lock:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        batch = self._buffer[:]
        self._buffer.clear()
        self._fs.append_daily_log_entries(batch)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._flush_locked()
        super().close()


def attach_firestore_daily_logs(
    firestore_logger: "FirestoreLogger",
    *,
    root_logger: logging.Logger | None = None,
) -> FirestoreDailyLogHandler:
    """Attach daily Firestore log handler to the root logger."""

    root = root_logger or logging.getLogger()
    min_level_name = __import__("os").environ.get("FIREBASE_LOG_LEVEL", "INFO").upper()
    min_level = getattr(logging, min_level_name, logging.INFO)

    handler = FirestoreDailyLogHandler(firestore_logger, min_level=min_level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    root.addHandler(handler)
    logging.getLogger(__name__).info(
        "Firestore daily log handler attached (bots/%s/daily_logs/{{date}}/entries)",
        firestore_logger.bot_id,
    )
    return handler
