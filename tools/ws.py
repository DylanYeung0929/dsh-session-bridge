#!/usr/bin/env python3
"""ws.py — minimal RFC 6455 WebSocket client, standard library only.

Why this exists
---------------
`dshctl.py watch` subscribes to DSH's live event stream at
``ws://127.0.0.1:<port>/api/remote.mux``, authenticated with the same
``dsh-auth-*`` cookie the GUI uses.  Depending on ``websockets`` or
``websocket-client`` would break the "runs anywhere Python 3 runs" promise of
this repo, so this module implements only what the mux needs: the client-side
Upgrade handshake, masked client frames, text-message reassembly, and the
ping/pong keepalive DSH relies on (the server pings every 2 s; a client that
never answers would be dropped while merely waiting for events).

Usage
-----
    sock = ws.connect(url, headers={"Cookie": f"{name}={value}"})
    sock.send_text(json.dumps({"type": "open", "streamId": sid, ...}))
    while True:
        event = json.loads(sock.recv_text())    # blocks until a message

``recv_text()`` always returns one whole message: ping frames are answered
inline, continuation frames are joined, pong frames are ignored, and a peer
close raises ``ConnectionClosed``.  Binary messages raise ``WebSocketError``
because the DSH mux only speaks JSON text.  Messages are capped at 32 MiB so a
corrupt length field cannot eat all memory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 32 * 1024 * 1024  # bytes, after reassembly
MAX_HEADERS = 64 * 1024         # bytes, handshake response

_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

_RESERVED = {"host", "upgrade", "connection",
             "sec-websocket-key", "sec-websocket-version"}


class WebSocketError(Exception):
    """Protocol or transport-level failure."""


class ConnectionClosed(WebSocketError):
    """The connection is gone; stop reading and close up."""


# --------------------------------------------------------------------------- #
# handshake
# --------------------------------------------------------------------------- #
def connect(url: str, headers: dict[str, str] | None = None,
            timeout: float = 10.0) -> "WebSocket":
    """Open ``url`` (``ws://`` or ``wss://``) and return a live :class:`WebSocket`.

    ``timeout`` covers DNS, TCP, TLS and the HTTP upgrade.  The socket then
    goes back to blocking mode, which is what a long-lived mux stream wants;
    per-call read deadlines belong to :meth:`WebSocket.recv_text`.
    """
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("ws", "wss"):
            raise WebSocketError(f"unsupported WebSocket URL scheme: {parts.scheme!r}")
        secure = parts.scheme.lower() == "wss"
        host = parts.hostname
        port = parts.port or (443 if secure else 80)
    except ValueError as err:  # e.g. host:notaport
        raise WebSocketError(f"bad WebSocket URL {url!r}: {err}") from err
    if not host:
        raise WebSocketError(f"bad WebSocket URL (no host): {url!r}")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    # Build (and validate) the request before any socket work, so a bad header
    # fails fast instead of leaving a half-open connection behind.
    request = _handshake_request(host, port, secure, target, key, headers)
    expected = base64.b64encode(
        hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as err:
        raise WebSocketError(f"cannot connect to {host}:{port}: {err}") from err
    try:
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        sock.sendall(request)
        status, response = _read_handshake(sock)
        if status != 101:
            raise WebSocketError(f"WebSocket handshake failed: HTTP {status}")
        if not hmac.compare_digest(response.get("sec-websocket-accept", ""), expected):
            raise WebSocketError("WebSocket handshake failed: bad Sec-WebSocket-Accept")
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        raise
    sock.settimeout(None)  # idle until recv_text() asks for a deadline
    return WebSocket(sock)


def _handshake_request(host: str, port: int, secure: bool, target: str,
                       key: str, headers: dict[str, str] | None) -> bytes:
    default_port = 443 if secure else 80
    host_header = f"[{host}]" if ":" in host else host
    if port != default_port:
        host_header += f":{port}"
    lines = [
        f"GET {target} HTTP/1.1",
        f"Host: {host_header}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for raw_name, raw_value in (headers or {}).items():
        name, value = str(raw_name).strip(), str(raw_value).strip()
        if not name or any(c in name for c in ":\r\n \t") or "\r" in value or "\n" in value:
            raise WebSocketError(f"invalid handshake header {name!r}: {value!r}")
        if name.lower() in _RESERVED:
            if name.lower() != "host":
                raise WebSocketError(f"refusing to override handshake header: {name}")
            lines[1] = f"Host: {value}"  # e.g. through a tunnel
            continue
        lines.append(f"{name}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def _read_handshake(sock: socket.socket) -> tuple[int, dict[str, str]]:
    """Read the HTTP response head one byte at a time so no frame byte is lost."""
    raw = bytearray()
    while not raw.endswith(b"\r\n\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise WebSocketError("connection closed during WebSocket handshake")
        raw += chunk
        if len(raw) > MAX_HEADERS:
            raise WebSocketError("WebSocket handshake response too large")
    lines = raw.decode("iso-8859-1").split("\r\n")
    status_line = lines[0]
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise WebSocketError(f"malformed HTTP status line: {status_line!r}")
    response: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise WebSocketError(f"malformed HTTP header: {line!r}")
        response[name.strip().lower()] = value.strip()
    return int(parts[1]), response


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #
class WebSocket:
    """One live RFC 6455 connection.  Create it with :func:`connect`."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._lock = threading.Lock()   # one writer at a time: text + pong reply
        self._closed = False
        self._frag: list[bytes] = []
        self._frag_size = 0

    def __enter__(self) -> "WebSocket":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._closed

    # -- sending ---------------------------------------------------------- #
    def send_text(self, text: str) -> None:
        """Send one masked text frame."""
        if not isinstance(text, str):
            raise TypeError("send_text() expects str")
        self._require_open()
        try:
            self._send_frame(_OP_TEXT, text.encode("utf-8"))
        except OSError as err:
            self._teardown()
            raise ConnectionClosed(f"websocket write failed: {err}") from err

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame (best effort) and drop the socket.  Idempotent."""
        if self._closed:
            return
        try:
            self._send_frame(_OP_CLOSE, _close_payload(code, reason))
        except (OSError, WebSocketError):
            pass
        finally:
            self._teardown()

    # -- receiving -------------------------------------------------------- #
    def recv_text(self, timeout: float | None = None) -> str:
        """Return the next whole text message, blocking until one arrives.

        ``timeout`` bounds each underlying socket read (``socket.timeout`` is
        raised when it expires); without it reads block forever, which is what
        a `watch` loop wants.  Ping frames are answered automatically, pong
        frames are ignored, and a close frame raises :class:`ConnectionClosed`.
        """
        self._require_open()
        restore = timeout is not None
        if restore:
            try:
                self._sock.settimeout(timeout)
            except OSError as err:
                self._teardown()
                raise ConnectionClosed(f"websocket is closed: {err}") from err
        try:
            return self._recv_message()
        finally:
            if restore:
                try:
                    self._sock.settimeout(None)
                except OSError:
                    pass

    def _recv_message(self) -> str:
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == _OP_PING:      # keepalive: must answer or get dropped
                self._reply(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:      # unsolicited pong: ignore
                continue
            if opcode == _OP_CLOSE:
                raise self._peer_closed(payload)
            if opcode == _OP_BINARY:
                raise WebSocketError("unexpected binary WebSocket frame (text expected)")
            if opcode == _OP_TEXT:
                if self._frag:
                    self._discard_fragments()
                    raise WebSocketError("text frame interleaved inside a fragmented message")
                if fin:
                    return _decode_text(payload)
                self._frag = [payload]
                self._frag_size = len(payload)
                continue
            if opcode == _OP_CONT:
                if not self._frag:
                    raise WebSocketError("continuation frame without a message in progress")
                self._frag.append(payload)
                self._frag_size += len(payload)
                if self._frag_size > MAX_MESSAGE:
                    self._discard_fragments()
                    raise WebSocketError(f"WebSocket message exceeds {MAX_MESSAGE} bytes")
                if fin:
                    data = b"".join(self._frag)
                    self._discard_fragments()
                    return _decode_text(data)
                continue
            raise WebSocketError(f"unsupported WebSocket opcode: {opcode:#x}")

    def _read_frame(self) -> tuple[bool, int, bytes]:
        head = self._recv_exact(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._recv_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._recv_exact(8))
        if length > MAX_MESSAGE:
            raise WebSocketError(f"WebSocket frame of {length} bytes exceeds {MAX_MESSAGE}")
        if opcode >= _OP_CLOSE and (not fin or length > 125):
            raise WebSocketError("fragmented or oversized WebSocket control frame")
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length else b""
        return fin, opcode, (_apply_mask(payload, mask) if mask else payload)

    def _recv_exact(self, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            try:
                chunk = self._sock.recv(remaining)
            except socket.timeout:
                raise  # a read deadline is the caller's business, not a close
            except OSError as err:
                self._teardown()
                raise ConnectionClosed(f"websocket read failed: {err}") from err
            if not chunk:
                self._teardown()
                raise ConnectionClosed("websocket closed by peer (EOF)")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    # -- frame writing ---------------------------------------------------- #
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        header = bytearray([0x80 | opcode])  # FIN always set: we never fragment
        if length < 126:
            header.append(0x80 | length)     # mask bit on: all client frames
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        frame = bytes(header) + mask + _apply_mask(payload, mask)
        with self._lock:
            self._sock.sendall(frame)

    def _reply(self, opcode: int, payload: bytes) -> None:
        try:
            self._send_frame(opcode, payload)
        except OSError as err:
            self._teardown()
            raise ConnectionClosed(f"websocket write failed: {err}") from err

    # -- state ------------------------------------------------------------ #
    def _require_open(self) -> None:
        if self._closed:
            raise ConnectionClosed("websocket is closed")

    def _discard_fragments(self) -> None:
        self._frag = []
        self._frag_size = 0

    def _peer_closed(self, payload: bytes) -> ConnectionClosed:
        code, reason = _parse_close(payload)
        try:
            self._send_frame(_OP_CLOSE, _close_payload(code, ""))  # echo, no reason
        except (OSError, WebSocketError):
            pass
        self._teardown()
        detail = f"code {code}" + (f": {reason}" if reason else "")
        return ConnectionClosed(f"websocket closed by peer ({detail})")

    def _teardown(self) -> None:
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)  # wake a recv blocked elsewhere
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# wire helpers
# --------------------------------------------------------------------------- #
def _apply_mask(payload: bytes, key: bytes) -> bytes:
    """XOR ``payload`` with the repeating 4-byte mask (masks are symmetric)."""
    if not payload:
        return b""
    stream = (key * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(stream, "big")).to_bytes(
        len(payload), "big")


def _close_payload(code: int, reason: str) -> bytes:
    code = int(code)
    if not 1000 <= code <= 4999:
        code = 1000  # 1005/1006 are local-only and must never hit the wire
    return struct.pack("!H", code) + reason.encode("utf-8")[:123]


def _parse_close(payload: bytes) -> tuple[int, str]:
    if len(payload) < 2:
        return 1005, ""  # "no status received"
    code = struct.unpack("!H", payload[:2])[0]
    return code, payload[2:].decode("utf-8", "replace")


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as err:
        raise WebSocketError(f"invalid UTF-8 in WebSocket text message: {err}") from err
