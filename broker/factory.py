"""Broker factory and module-level singleton (FIX only)."""

from __future__ import annotations

import os
from typing import Optional

from broker.base import Broker
from broker.ctrader_fix import CTraderFixBroker

_broker: Optional[Broker] = None


def create_broker(
    symbols: list[str],
    cache_dir: str,
) -> Broker:
    return CTraderFixBroker(symbols=symbols, cache_dir=cache_dir)


def get_broker() -> Broker:
    if _broker is None:
        raise RuntimeError("Broker not initialized — call init_broker() first")
    return _broker


def init_broker(symbols: list[str], cache_dir: str) -> Broker:
    global _broker
    _broker = create_broker(symbols, cache_dir)
    return _broker


def load_dotenv(path: str) -> None:
    """Minimal .env loader (no external dependency)."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
