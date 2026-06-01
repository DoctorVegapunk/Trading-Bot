"""Abstract broker interface used by live_trader.py."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class Tick:
    bid: float
    ask: float


@dataclass
class OpenPosition:
    symbol: str
    direction: str  # "BUY" or "SELL"
    volume: float     # lots
    ticket: str = ""


class Broker(ABC):
    """Exchange connectivity: candles, account, orders, positions."""

    @abstractmethod
    def connect(self) -> bool:
        ...

    @abstractmethod
    def shutdown(self) -> None:
        ...

    @abstractmethod
    def ensure_connected(self) -> bool:
        ...

    @abstractmethod
    def get_bid_ask_candles(self, symbol: str, count: int) -> Optional[pd.DataFrame]:
        ...

    @abstractmethod
    def get_tick(self, symbol: str) -> Optional[Tick]:
        ...

    @abstractmethod
    def get_account_balance(self) -> float:
        ...

    @abstractmethod
    def get_account_currency(self) -> str:
        ...

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        direction: str,
        lot_size: float,
        sl_price: float,
        tp_price: float,
        comment: str = "XGB_Bot",
        dry_run: bool = False,
    ) -> bool:
        ...

    @abstractmethod
    def get_bot_positions(self, magic: int = 20250516) -> list[OpenPosition]:
        ...

    def refresh_positions(self) -> None:
        """Optional hook to refresh open positions from the broker."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...
