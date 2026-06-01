"""MetaTrader 5 broker adapter (Windows + MT5 terminal)."""

from __future__ import annotations

import logging
import socket
import time
from typing import Optional

import pandas as pd

from broker.base import Broker, OpenPosition, Tick

log = logging.getLogger(__name__)

TIMEFRAME_H1 = None  # set on connect


class MT5Broker(Broker):
    def __init__(self):
        self._mt5 = None

    @property
    def name(self) -> str:
        return "MetaTrader 5"

    def connect(self) -> bool:
        import MetaTrader5 as mt5
        self._mt5 = mt5
        global TIMEFRAME_H1
        if not mt5.initialize():
            log.error("MT5 initialize() failed: %s", mt5.last_error())
            return False
        info = mt5.terminal_info()
        account = mt5.account_info()
        log.info(
            "MT5 connected: %s | Account: %s | Balance: %.2f %s",
            info.name,
            account.login,
            account.balance,
            account.currency,
        )
        return True

    def shutdown(self) -> None:
        if self._mt5:
            self._mt5.shutdown()

    def ensure_connected(self) -> bool:
        if not _check_internet():
            return False
        mt5 = self._mt5
        info = mt5.terminal_info()
        if info is not None and info.connected:
            return True
        log.warning("MT5 broker connection lost — attempting reconnect...")
        mt5.shutdown()
        time.sleep(3)
        return self.connect()

    def get_bid_ask_candles(self, symbol: str, count: int) -> Optional[pd.DataFrame]:
        mt5 = self._mt5
        import MetaTrader5 as mt5_mod
        tf = mt5_mod.TIMEFRAME_H1
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            log.error("Failed to get rates for %s: %s", symbol, mt5.last_error())
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time").sort_index()
        df = df[["open", "high", "low", "close", "tick_volume"]].rename(
            columns={"tick_volume": "volume"}
        )
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            symbol_info = mt5.symbol_info(symbol)
            point = symbol_info.point if symbol_info else 0.00001
            spread_price = point * 10
        else:
            spread_price = tick.ask - tick.bid
        half = spread_price / 2.0
        df["ask_open"] = df["open"] + half
        df["ask_high"] = df["high"] + half
        df["ask_low"] = df["low"] + half
        df["ask_close"] = df["close"] + half
        symbol_info = mt5.symbol_info(symbol)
        point = symbol_info.point if symbol_info else 0.00001
        pip_size = point * 10
        df["spread_pips"] = spread_price / pip_size
        return df

    def get_tick(self, symbol: str) -> Optional[Tick]:
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return Tick(bid=tick.bid, ask=tick.ask)

    def get_account_balance(self) -> float:
        info = self._mt5.account_info()
        return info.equity if info else 0.0

    def get_account_currency(self) -> str:
        info = self._mt5.account_info()
        return info.currency if info else "USD"

    def get_bot_positions(self, magic: int = 20250516) -> list[OpenPosition]:
        all_open = self._mt5.positions_get()
        out: list[OpenPosition] = []
        for p in all_open or []:
            if p.magic != magic:
                continue
            out.append(OpenPosition(
                symbol=p.symbol,
                direction="BUY" if p.type == 0 else "SELL",
                volume=p.volume,
                ticket=str(p.ticket),
            ))
        return out

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
        mt5 = self._mt5
        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log.error("Cannot get tick for %s", symbol)
            return False
        price = tick.ask if direction == "BUY" else tick.bid
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return False
        lot_step = symbol_info.volume_step
        lot_min = symbol_info.volume_min
        lot_size = max(round(lot_size / lot_step) * lot_step, lot_min)
        lot_size = round(lot_size, 2)
        digits = symbol_info.digits
        sl_price = round(sl_price, digits)
        tp_price = round(tp_price, digits)
        if dry_run:
            log.info(
                "[DRY RUN] Would place %s %.2f lots %s @ ~%.5f | SL=%.5f | TP=%.5f",
                direction, lot_size, symbol, price, sl_price, tp_price,
            )
            return True
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot_size,
            "type": order_type,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "deviation": 20,
            "magic": 20250516,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(
                "Order filled: %s %.2f lots %s @ %.5f | SL=%.5f | TP=%.5f | #%s",
                direction, lot_size, symbol, result.price, sl_price, tp_price, result.order,
            )
            return True
        log.error(
            "Order failed: retcode=%s comment=%s (%s %.2f %s)",
            result.retcode, result.comment, direction, lot_size, symbol,
        )
        return False


def _check_internet(host: str = "8.8.8.8", port: int = 53, timeout: int = 5) -> bool:
    try:
        socket.setdefaulttimeout(timeout)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((host, port))
        return True
    except OSError:
        return False
