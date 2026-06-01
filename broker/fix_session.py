"""
Threaded FIX 4.4 session (one QUOTE or TRADE connection).
"""

from __future__ import annotations

import logging
import socket
import ssl
import threading
import time
from collections import deque
from typing import Callable, Optional

from broker.fix_protocol import (
    SOH,
    build_fix_message,
    parse_messages,
    utc_timestamp,
)

log = logging.getLogger(__name__)

MsgHandler = Callable[[dict[str, str], str], None]


class FixSession:
    """Single cTrader FIX session over TCP (+ optional TLS)."""

    def __init__(
        self,
        *,
        name: str,
        host: str,
        port: int,
        use_ssl: bool,
        sender_comp_id: str,
        target_comp_id: str,
        target_sub_id: str,
        sender_sub_id: str,
        username: str,
        password: str,
        heartbeat_sec: int = 30,
        on_message: Optional[MsgHandler] = None,
    ):
        self.name = name
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.sender_comp_id = sender_comp_id
        self.target_comp_id = target_comp_id
        self.target_sub_id = target_sub_id
        self.sender_sub_id = sender_sub_id
        self.username = username
        self.password = password
        self.heartbeat_sec = heartbeat_sec
        self.on_message = on_message

        self._sock: Optional[ssl.SSLSocket | socket.socket] = None
        self._seq = 1
        self._logged_on = False
        self._lock = threading.Lock()
        self._recv_buf = b""
        self._stop = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._heartbeater: Optional[threading.Thread] = None
        self._last_recv = time.time()
        self._recent_raw: deque[str] = deque(maxlen=50)

    @property
    def logged_on(self) -> bool:
        return self._logged_on

    def connect_and_logon(self, timeout: float = 30.0) -> bool:
        self._stop.clear()
        try:
            raw_sock = socket.create_connection((self.host, self.port), timeout=timeout)
            if self.use_ssl:
                ctx = ssl.create_default_context()
                self._sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)
            else:
                self._sock = raw_sock
            self._sock.settimeout(1.0)
        except OSError as exc:
            log.error("%s: TCP connect failed: %s", self.name, exc)
            return False

        self._reader = threading.Thread(target=self._read_loop, daemon=True, name=f"fix-{self.name}")
        self._reader.start()
        self._heartbeater = threading.Thread(target=self._heartbeat_loop, daemon=True, name=f"fix-hb-{self.name}")
        self._heartbeater.start()

        if not self._send_logon():
            self.disconnect()
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._logged_on:
                log.info("%s: logged on", self.name)
                return True
            time.sleep(0.1)
        log.error("%s: logon timeout", self.name)
        self.disconnect()
        return False

    def disconnect(self) -> None:
        self._stop.set()
        if self._logged_on and self._sock:
            try:
                self.send_raw_msg_type("5", [])
            except OSError:
                pass
        self._logged_on = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def send_application(self, msg_type: str, body_fields: list[tuple[int, Any]]) -> bool:
        if not self._logged_on:
            log.warning("%s: not logged on, cannot send %s", self.name, msg_type)
            return False
        return self.send_raw_msg_type(msg_type, body_fields)

    def send_raw_msg_type(self, msg_type: str, body_fields: list[tuple[int, Any]]) -> bool:
        with self._lock:
            if not self._sock:
                return False
            msg = build_fix_message(
                msg_type=msg_type,
                sender_comp_id=self.sender_comp_id,
                target_comp_id=self.target_comp_id,
                target_sub_id=self.target_sub_id,
                sender_sub_id=self.sender_sub_id,
                seq_num=self._seq,
                body_fields=body_fields,
            )
            self._seq += 1
            try:
                self._sock.sendall(msg)
                return True
            except OSError as exc:
                log.error("%s: send failed: %s", self.name, exc)
                self._logged_on = False
                return False

    def _send_logon(self) -> bool:
        fields = [
            (98, 0),
            (108, self.heartbeat_sec),
            (141, "Y"),
            (553, self.username),
            (554, self.password),
        ]
        return self.send_raw_msg_type("A", fields)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._sock:
                    break
                chunk = self._sock.recv(65536)
                if not chunk:
                    log.warning("%s: connection closed by server", self.name)
                    self._logged_on = False
                    break
                self._last_recv = time.time()
                self._recv_buf += chunk
                msgs, self._recv_buf = parse_messages(self._recv_buf)
                for tags in msgs:
                    raw = SOH.join(f"{k}={v}" for k, v in tags.items())
                    self._recent_raw.append(raw)
                    self._dispatch(tags, raw)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    log.warning("%s: read error: %s", self.name, exc)
                    self._logged_on = False
                break

    def _dispatch(self, tags: dict[str, str], raw: str) -> None:
        msg_type = tags.get("35", "")
        if msg_type == "A":
            self._logged_on = True
        elif msg_type == "5":
            text = tags.get("58", "logout")
            log.warning("%s: logout: %s", self.name, text)
            self._logged_on = False
        elif msg_type == "0":
            pass  # heartbeat
        elif msg_type == "1":
            test_id = tags.get("112", "")
            self.send_raw_msg_type("0", [])
            if test_id:
                pass
        elif msg_type == "3":
            log.error("%s: session reject: %s", self.name, tags)
        if self.on_message:
            self.on_message(tags, raw)

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.heartbeat_sec)
            if self._stop.is_set():
                break
            if not self._logged_on:
                continue
            idle = time.time() - self._last_recv
            if idle >= self.heartbeat_sec:
                self.send_raw_msg_type("0", [])
