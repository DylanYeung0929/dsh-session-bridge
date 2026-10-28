#!/usr/bin/env python3
"""Offline unit tests for tools/ws.py — a real socket server on 127.0.0.1."""
import base64
import contextlib
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import ws  # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --------------------------------------------------------------------------- #
# a throw-away RFC 6455 server: one connection per test, no dependencies
# --------------------------------------------------------------------------- #
def _read_until(conn, sep, limit=64 * 1024):
    buf = bytearray()
    while not buf.endswith(sep):
        chunk = conn.recv(1)
        if not chunk:
            raise EOFError("client closed during HTTP handshake")
        buf += chunk
        if len(buf) > limit:
            raise ValueError("handshake request too large")
    return bytes(buf)


def _parse_request(raw):
    lines = raw.decode("iso-8859-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"bad request header: {line!r}")
        headers[name.strip().lower()] = value.strip()
    return lines[0], headers


def frame(opcode, payload=b"", fin=True, masked=False):
    """Build a server frame; `masked=True` exercises the tolerant reader."""
    if isinstance(payload, str):
        payload = payload.encode()
    head = bytearray([(0x80 if fin else 0x00) | opcode])
    bit = 0x80 if masked else 0x00
    size = len(payload)
    if size < 126:
        head.append(bit | size)
    elif size < 65536:
        head.append(bit | 126)
        head += struct.pack("!H", size)
    else:
        head.append(bit | 127)
        head += struct.pack("!Q", size)
    if not masked:
        return bytes(head) + payload
    key = b"\x01\x02\x03\x04"
    return bytes(head) + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def read_frame(conn):
    """Server-side frame parser; returns (fin, opcode, payload, masked)."""
    head = _read_exact(conn, 2)
    fin = bool(head[0] & 0x80)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    size = head[1] & 0x7F
    if size == 126:
        (size,) = struct.unpack("!H", _read_exact(conn, 2))
    elif size == 127:
        (size,) = struct.unpack("!Q", _read_exact(conn, 8))
    key = _read_exact(conn, 4) if masked else None
    payload = _read_exact(conn, size) if size else b""
    if key:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, masked


def _read_exact(conn, size):
    buf = bytearray()
    while len(buf) < size:
        chunk = conn.recv(size - len(buf))
        if not chunk:
            raise EOFError("client closed mid-frame")
        buf += chunk
    return bytes(buf)


class FakeServer:
    def __init__(self, listener, port, handler, response):
        self.listener = listener
        self.port = port
        self.handler = handler
        self.response = response    # canned handshake reply for rejection tests
        self.request_line = None
        self.headers = {}
        self.error = None

    def run(self):
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        try:
            with conn:
                conn.settimeout(3)
                self.request_line, self.headers = _parse_request(
                    _read_until(conn, b"\r\n\r\n"))
                if self.response is not None:
                    conn.sendall(self.response)
                    return
                accept = base64.b64encode(hashlib.sha1(
                    (self.headers["sec-websocket-key"] + GUID).encode()).digest()).decode()
                conn.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\n"
                    b"Connection: Upgrade\r\n"
                    + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
                self.handler(conn, self)
        except Exception as err:  # surfaced to the test by serve()
            self.error = err


@contextlib.contextmanager
def serve(handler, response=None):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    srv = FakeServer(listener, listener.getsockname()[1], handler, response)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        listener.close()
        thread.join(5)
        if srv.error is not None and sys.exc_info()[0] is None:
            raise srv.error


def wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def url(srv, path="/api/remote.mux"):
    return f"ws://127.0.0.1:{srv.port}{path}"


# --------------------------------------------------------------------------- #
class HandshakeTest(unittest.TestCase):
    def test_sends_cookie_and_computes_accept(self):
        def handler(conn, srv):
            _fin, opcode, payload, _masked = read_frame(conn)
            self.assertEqual(opcode, 0x1)
            conn.sendall(frame(0x1, payload))

        with serve(handler) as srv:
            with ws.connect(url(srv, "/api/remote.mux?since=1"),
                            headers={"Cookie": "dsh-auth-abc=secret",
                                     "Origin": "http://127.0.0.1"}) as sock:
                sock.send_text(json.dumps({"type": "open", "streamId": "s1"}))
                self.assertEqual(json.loads(sock.recv_text(5)),
                                 {"type": "open", "streamId": "s1"})

        self.assertEqual(srv.request_line,
                         "GET /api/remote.mux?since=1 HTTP/1.1")
        self.assertEqual(srv.headers["cookie"], "dsh-auth-abc=secret")
        self.assertEqual(srv.headers["origin"], "http://127.0.0.1")
        self.assertEqual(srv.headers["upgrade"].lower(), "websocket")
        self.assertEqual(srv.headers["connection"].lower(), "upgrade")
        self.assertEqual(srv.headers["sec-websocket-version"], "13")
        key = base64.b64decode(srv.headers["sec-websocket-key"])
        self.assertEqual(len(key), 16)  # RFC 6455 nonce is 16 random bytes

    def test_bad_status_raises(self):
        reply = b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n"
        with serve(None, response=reply) as srv:
            with self.assertRaises(ws.WebSocketError) as ctx:
                ws.connect(url(srv), timeout=5)
        self.assertIn("401", str(ctx.exception))

    def test_bad_accept_raises(self):
        reply = (b"HTTP/1.1 101 Switching Protocols\r\n"
                 b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                 b"Sec-WebSocket-Accept: AAAAAAAAAAAAAAAAAAAAAAAAAAA=\r\n\r\n")
        with serve(None, response=reply) as srv:
            with self.assertRaises(ws.WebSocketError) as ctx:
                ws.connect(url(srv), timeout=5)
        self.assertIn("accept", str(ctx.exception).lower())

    def test_refuses_to_override_websocket_key(self):
        with self.assertRaises(ws.WebSocketError):
            ws.connect("ws://127.0.0.1:1/api/remote.mux",
                       headers={"Sec-WebSocket-Key": "not-ours"})


class MessageTest(unittest.TestCase):
    def test_multiple_messages_survive_one_read(self):
        def handler(conn, srv):
            conn.sendall(frame(0x1, b"one") + frame(0x1, b"two") + frame(0x1, b"three"))

        with serve(handler) as srv, ws.connect(url(srv), timeout=5) as sock:
            self.assertEqual([sock.recv_text(5) for _ in range(3)],
                             ["one", "two", "three"])

    def test_extended_length_frames_roundtrip(self):
        payloads = ["a" * 300, "b" * 70000, "😀" * 10]  # 126-byte, 127-byte, UTF-8

        def handler(conn, srv):
            for _ in payloads:
                _fin, _op, payload, _masked = read_frame(conn)
                conn.sendall(frame(0x1, payload))

        with serve(handler) as srv, ws.connect(url(srv), timeout=5) as sock:
            for payload in payloads:
                sock.send_text(payload)
                self.assertEqual(sock.recv_text(5), payload)

    def test_fragments_are_reassembled_and_ping_answered(self):
        def handler(conn, srv):
            conn.sendall(frame(0x1, b"hel", fin=False))
            conn.sendall(frame(0x0, b"lo ", fin=False))
            conn.sendall(frame(0x9, b"keepalive"))       # control frame between fragments
            _fin, opcode, payload, masked = read_frame(conn)
            srv.pong = (opcode, payload, masked)
            conn.sendall(frame(0x0, b"world", fin=True))

        with serve(handler) as srv, ws.connect(url(srv), timeout=5) as sock:
            self.assertEqual(sock.recv_text(5), "hello world")
        self.assertEqual(srv.pong, (0xA, b"keepalive", True))  # masked pong

    def test_pong_frames_are_ignored(self):
        def handler(conn, srv):
            conn.sendall(frame(0xA, b"stray") + frame(0x1, b"ok"))

        with serve(handler) as srv, ws.connect(url(srv), timeout=5) as sock:
            self.assertEqual(sock.recv_text(5), "ok")

    def test_masked_server_frame_is_accepted(self):
        def handler(conn, srv):
            conn.sendall(frame(0x1, "masked!".encode(), masked=True))

        with serve(handler) as srv, ws.connect(url(srv), timeout=5) as sock:
            self.assertEqual(sock.recv_text(5), "masked!")

    def test_binary_frame_raises(self):
        def handler(conn, srv):
            conn.sendall(frame(0x2, b"\x00\x01"))

        with serve(handler) as srv, ws.connect(url(srv), timeout=5) as sock:
            with self.assertRaises(ws.WebSocketError) as ctx:
                sock.recv_text(5)
        self.assertNotIsInstance(ctx.exception, ws.ConnectionClosed)

    def test_idle_loop_answers_pings_until_a_message_arrives(self):
        def handler(conn, srv):
            for _ in range(4):
                conn.sendall(frame(0x9, b"ping"))
                _fin, opcode, _payload, masked = read_frame(conn)
                self.assertEqual(opcode, 0xA)
                self.assertTrue(masked)
                srv.pongs += 1
                time.sleep(0.02)
            conn.sendall(frame(0x1, b"late"))   # never exit the loop early

        with serve(handler) as srv:
            srv.pongs = 0                       # before the client can connect
            with ws.connect(url(srv), timeout=5) as sock:
                self.assertEqual(sock.recv_text(5), "late")
        self.assertEqual(srv.pongs, 4)

    def test_recv_timeout_honours_deadline(self):
        def handler(conn, srv):
            time.sleep(0.4)  # say nothing at all

        with serve(handler) as srv, ws.connect(url(srv), timeout=5) as sock:
            with self.assertRaises(socket.timeout):
                sock.recv_text(0.1)


class CloseTest(unittest.TestCase):
    def test_peer_close_raises_and_wins(self):
        def handler(conn, srv):
            conn.sendall(frame(0x8, struct.pack("!H", 1000) + b"bye"))
            _fin, opcode, payload, masked = read_frame(conn)
            srv.close_reply = (opcode, payload, masked)

        with serve(handler) as srv, ws.connect(url(srv), timeout=5) as sock:
            with self.assertRaises(ws.ConnectionClosed) as ctx:
                sock.recv_text(5)
            self.assertEqual(ctx.exception.args[0].split(":")[0],
                             "websocket closed by peer (code 1000")
            self.assertTrue(sock.closed)
            with self.assertRaises(ws.ConnectionClosed):
                sock.recv_text(1)   # closed stays closed

        self.assertEqual(srv.close_reply, (0x8, struct.pack("!H", 1000), True))

    def test_client_close_sends_close_frame(self):
        def handler(conn, srv):
            _fin, opcode, payload, masked = read_frame(conn)
            srv.received = (opcode, payload, masked)

        with serve(handler) as srv:
            sock = ws.connect(url(srv), timeout=5)
            sock.close(1001, "going away")
            self.assertTrue(sock.closed)
            sock.close()                      # idempotent
            self.assertTrue(wait_for(lambda: getattr(srv, "received", None) is not None))
        self.assertEqual(srv.received, (0x8, struct.pack("!H", 1001) + b"going away", True))

    def test_eof_without_close_frame_raises(self):
        def handler(conn, srv):
            conn.shutdown(socket.SHUT_RDWR)   # bare TCP goodbye

        with serve(handler) as srv, ws.connect(url(srv), timeout=5) as sock:
            with self.assertRaises(ws.ConnectionClosed):
                sock.recv_text(5)


if __name__ == "__main__":
    unittest.main()
