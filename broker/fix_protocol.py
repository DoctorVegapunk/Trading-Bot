"""
Minimal FIX 4.4 message builder/parser for cTrader (Pepperstone).

Spec: https://help.ctrader.com/fix/specification/
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional


SOH = "\x01"


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H:%M:%S")


def checksum(payload: str) -> str:
    total = sum(ord(c) for c in payload)
    return str(total % 256).zfill(3)


def build_fix_message(
    *,
    msg_type: str,
    sender_comp_id: str,
    target_comp_id: str,
    target_sub_id: str,
    sender_sub_id: str,
    seq_num: int,
    body_fields: list[tuple[int, Any]],
) -> bytes:
    """Construct a FIX 4.4 message with standard header and trailer."""
    body = "".join(f"{tag}={value}|" for tag, value in body_fields)

    header_fields = (
        f"35={msg_type}|"
        f"49={sender_comp_id}|"
        f"56={target_comp_id}|"
        f"57={target_sub_id}|"
        f"50={sender_sub_id}|"
        f"34={seq_num}|"
        f"52={utc_timestamp()}|"
    )
    header_and_body = header_fields + body
    body_length = len(header_and_body)
    prefix = f"8=FIX.4.4|9={body_length}|"
    without_checksum = prefix + header_and_body
    csum = checksum(without_checksum.replace("|", SOH))
    raw = (without_checksum + f"10={csum}|").replace("|", SOH)
    return raw.encode("ascii")


def parse_messages(buffer: bytes) -> tuple[list[dict[str, str]], bytes]:
    """
    Split a byte buffer into complete FIX messages.
    Returns (list of tag dicts, remaining bytes).
    """
    data = buffer
    messages: list[dict[str, str]] = []
    prefix = b"8=FIX.4.4" + SOH.encode()

    while True:
        start = data.find(prefix)
        if start < 0:
            break
        if start > 0:
            data = data[start:]
            start = 0
        tag9 = data.find(b"9=", 0, 32)
        if tag9 < 0:
            break
        soh_after_len = data.find(SOH.encode(), tag9)
        if soh_after_len < 0:
            break
        try:
            body_len = int(data[tag9 + 2 : soh_after_len].decode())
        except ValueError:
            break
        body_start = soh_after_len + 1
        body_end = body_start + body_len
        if len(data) < body_end:
            break
        chk = data.find(b"10=", body_end)
        if chk < 0:
            break
        end = data.find(SOH.encode(), chk)
        if end < 0:
            break
        end += 1
        chunk = data[:end].decode("ascii", errors="replace")
        messages.append(_parse_single(chunk))
        data = data[end:]

    return messages, data


def _parse_single(raw: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for part in raw.split(SOH):
        if "=" not in part:
            continue
        tag, _, value = part.partition("=")
        if tag.isdigit():
            tags[tag] = value
    return tags


def get_tag(msg: dict[str, str], tag: str, default: Optional[str] = None) -> Optional[str]:
    return msg.get(tag, default)


def parse_repeating_symbols(msg: dict[str, str]) -> list[tuple[int, str, int]]:
    """
    Parse Security List (35=y) repeating group: symbol id, name, digits.
    Uses raw message reconstruction from common tag patterns.
    """
    # Security list packs multiple 55= / 1007= / 1008= in one message;
    # full parse needs raw body — handled in client via regex on raw string.
    return []


def parse_security_list_raw(raw: str) -> dict[str, int]:
    """Extract SymbolName -> Symbol id from a Security List message."""
    mapping: dict[str, int] = {}
    names = re.findall(r"1007=([^" + SOH + r"|]+)", raw)
    ids = re.findall(r"(?:^|" + SOH + r")55=(\d+)", raw)
    # ids/names may not align 1:1 with simple regex — walk in order
    parts = raw.split(SOH)
    current_id: Optional[int] = None
    for part in parts:
        if part.startswith("55="):
            try:
                current_id = int(part[3:])
            except ValueError:
                current_id = None
        elif part.startswith("1007=") and current_id is not None:
            name = part[5:].strip()
            mapping[name.upper()] = current_id
            current_id = None
    return mapping


def parse_md_prices(msg: dict[str, str], raw: str) -> tuple[Optional[float], Optional[float]]:
    """Best bid/ask from Market Data Snapshot (W) or Incremental (X)."""
    sep = SOH
    bids = re.findall(r"269=0" + sep + r"270=([\d.eE+-]+)", raw)
    asks = re.findall(r"269=1" + sep + r"270=([\d.eE+-]+)", raw)
    bid = float(bids[-1]) if bids else None
    ask = float(asks[-1]) if asks else None
    return bid, ask
