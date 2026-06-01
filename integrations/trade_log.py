"""Fan-out trade logging: Excel + optional Firestore."""

from __future__ import annotations

from integrations.firestore_logger import FirestoreLogger


class CompositeTradeLogger:
    def __init__(self, excel_logger, firestore: FirestoreLogger | None = None):
        self._excel = excel_logger
        self._firestore = firestore or FirestoreLogger()

    def log_trade(self, row_data: dict) -> None:
        self._excel.log_trade(row_data)
        self._firestore.log_trade(row_data)
