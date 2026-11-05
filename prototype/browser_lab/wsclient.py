"""A minimal WebSocket client (standard library only): the GPT-Live sideband close, and (WebSocket) the
caller-role test's text conversation with an ElevenAgents agent.

    close_live_session(session_id, api_key) attaches to wss://api.openai.com/v1/live/sessions/{id}/attach,
    sends session.close and waits for session.closed (official server controls guide, read 2026-10-07).

Text frames only, masked as RFC 6455 requires for clients. Written from the docs; not exercised against a real
account. The key is sent only in the Authorization header of the upgrade request and never logged.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time


def _frame(payload: bytes, opcode: int = 0x1) -> bytes:
    mask = os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def _recv_frame(sock) -> tuple[int, bytes]:
    b1, b2 = _recv_exact(sock, 2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if b2 & 0x80 else None
    data = _recv_exact(sock, n)
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return b1 & 0x0F, data


def close_live_session(session_id: str, api_key: str, timeout_s: float = 5.0, host: str = "api.openai.com") -> dict:
    """Returns {"closed": bool, "event": dict|None, "error": str|None}. Never raises."""
    path = f"/v1/live/sessions/{session_id}/attach"
    try:
        raw = socket.create_connection((host, 443), timeout=timeout_s)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"Authorization: Bearer {api_key}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            return {"closed": False, "event": None, "error": head.split(b"\r\n", 1)[0].decode(errors="replace")}
        sock.sendall(_frame(json.dumps({"type": "session.close"}).encode()))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            opcode, data = _recv_frame(sock)
            if opcode == 0x8:
                break
            if opcode == 0x9:
                sock.sendall(_frame(data, 0xA))
                continue
            if opcode == 0x1:
                ev = json.loads(data.decode())
                if ev.get("type") == "session.closed":
                    return {"closed": True, "event": ev, "error": None}
        return {"closed": False, "event": None, "error": "session.closed not received"}
    except Exception as exc:   # noqa: BLE001 - reported, not raised
        return {"closed": False, "event": None, "error": type(exc).__name__}


class WebSocket:
    """A text-frame WebSocket connection to a wss:// URL (2026-11-05: the caller-role test talks to an ElevenAgents
    agent from the server, in text). Pings are answered; messages split into frames are put together."""

    def __init__(self, url: str, timeout_s: float = 15.0, headers: dict | None = None) -> None:
        import urllib.parse
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("wss", "ws"):
            raise ValueError(f"not a WebSocket URL: {u.scheme}")
        host, port = u.hostname or "", u.port or (443 if u.scheme == "wss" else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        raw = socket.create_connection((host, port), timeout=timeout_s)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if u.scheme == "wss" else raw
        key = base64.b64encode(os.urandom(16)).decode()
        extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            got = self.sock.recv(1)
            if not got:
                raise ConnectionError("closed during the handshake")
            head += got
        first = head.split(b"\r\n", 1)[0]
        if b" 101 " not in first + b" ":
            self.sock.close()
            raise ConnectionError(first.decode(errors="replace"))
        self.closed = False

    def send_json(self, obj: dict) -> None:
        self.sock.sendall(_frame(json.dumps(obj, ensure_ascii=False).encode("utf-8")))

    def recv_json(self, timeout_s: float) -> dict | None:
        """The next message as a dict; None when nothing came in time. Raises ConnectionError once closed."""
        if self.closed:
            raise ConnectionError("closed")
        deadline = time.monotonic() + timeout_s
        parts = b""
        while True:
            left = deadline - time.monotonic()
            if left <= 0 and not parts:
                return None
            self.sock.settimeout(max(0.05, left) if not parts else 15.0)
            try:
                b1 = _recv_exact(self.sock, 1)[0]
            except (socket.timeout, TimeoutError):
                if parts:
                    raise ConnectionError("a message stopped half way") from None
                return None
            self.sock.settimeout(15.0)
            b2 = _recv_exact(self.sock, 1)[0]
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", _recv_exact(self.sock, 2))[0]
            elif n == 127:
                n = struct.unpack(">Q", _recv_exact(self.sock, 8))[0]
            mask = _recv_exact(self.sock, 4) if b2 & 0x80 else None
            data = _recv_exact(self.sock, n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            opcode, fin = b1 & 0x0F, bool(b1 & 0x80)
            if opcode == 0x8:
                self.closed = True
                code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else None
                raise ConnectionError(f"closed by the other side (code {code}" +
                                      (f"：{data[2:].decode(errors='replace')}" if len(data) > 2 else "") + ")")
            if opcode == 0x9:
                self.sock.sendall(_frame(data, 0xA))
                continue
            if opcode == 0xA:
                continue
            parts += data
            if not fin:
                continue
            try:
                return json.loads(parts.decode("utf-8"))
            except ValueError:
                parts = b""
                continue

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.sock.sendall(_frame(struct.pack(">H", 1000), 0x8))
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass
