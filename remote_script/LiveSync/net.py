# -*- coding: utf-8 -*-
"""Tiny dependency-free WebSocket client that runs on a background thread.

Live's embedded Python has no third-party packages, so this implements just
enough of RFC 6455 (client handshake, masked text frames, ping/pong, close,
fragmented reads) over the standard library. The Live Object Model must only
be touched from Live's main thread, so this class never calls back into the
engine: it exchanges plain dicts through two thread-safe queues and the engine
drains the inbox on every tick.
"""
from __future__ import absolute_import, print_function, unicode_literals

import base64
import json
import os
import random
import select
import socket
import struct
import threading
import time

try:
    import queue
except ImportError:  # pragma: no cover
    import Queue as queue  # type: ignore

try:
    from urllib.parse import urlparse
except ImportError:  # pragma: no cover
    from urlparse import urlparse  # type: ignore

try:
    import ssl
except Exception:  # pragma: no cover - some Live builds ship without _ssl
    ssl = None


class HandshakeError(Exception):
    pass


def _apply_mask(payload, mask):
    """XOR payload with the 4-byte mask using big-int math (fast in pure Python)."""
    n = len(payload)
    if n == 0:
        return b""
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


class WebSocketConnection(object):
    """One blocking connection. Used only from the network thread."""

    def __init__(self, url, timeout=8.0):
        self.url = url
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise HandshakeError("Server address must start with ws:// or wss://")
        self.secure = parsed.scheme == "wss"
        self.host = parsed.hostname
        self.port = parsed.port or (443 if self.secure else 80)
        self.path = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.secure:
            if ssl is None:
                raw.close()
                raise HandshakeError("This copy of Live can't make secure (wss://) connections. Use a ws:// server address.")
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        else:
            self.sock = raw
        self._buf = b""
        self._fragments = []
        self._handshake(timeout)
        self.sock.settimeout(None)
        self.sock.setblocking(False)

    def _handshake(self, timeout):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = self.host if self.port in (80, 443) else "%s:%d" % (self.host, self.port)
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: LiveSync\r\n\r\n" % (self.path, host_header, key)
        )
        self.sock.settimeout(timeout)
        self.sock.sendall(req.encode("ascii"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise HandshakeError("The server closed the connection during setup.")
            data += chunk
            if len(data) > 65536:
                raise HandshakeError("Unexpected reply from the server.")
        head, rest = data.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise HandshakeError("The server refused the connection (%s)." % status.decode("latin-1"))
        self._buf = rest

    # -- frames -----------------------------------------------------------
    def _frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        return bytes(header) + _apply_mask(payload, mask)

    def send_text(self, text):
        self._sendall(self._frame(0x1, text.encode("utf-8")))

    def send_ping(self):
        self._sendall(self._frame(0x9, b"livesync"))

    def close(self):
        try:
            self._sendall(self._frame(0x8, struct.pack("!H", 1000)))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def _sendall(self, data):
        view = memoryview(data)
        while len(view):
            try:
                sent = self.sock.send(view)
                view = view[sent:]
            except (BlockingIOError, InterruptedError):
                select.select([], [self.sock], [], 1.0)
            except Exception as e:
                if ssl is not None and isinstance(e, (ssl.SSLWantWriteError, ssl.SSLWantReadError)):
                    select.select([], [self.sock], [], 1.0)
                    continue
                raise

    def fileno(self):
        return self.sock.fileno()

    def pending(self):
        return bool(self._buf) or (self.secure and self.sock.pending() > 0)

    def read_available(self):
        """Read whatever is available; return list of complete text messages."""
        while True:
            try:
                chunk = self.sock.recv(262144)
            except (BlockingIOError, InterruptedError):
                break
            except Exception as e:
                if ssl is not None and isinstance(e, (ssl.SSLWantReadError, ssl.SSLWantWriteError)):
                    break
                raise
            if not chunk:
                raise ConnectionError("Connection closed by server")
            self._buf += chunk
            if len(chunk) < 262144:
                break
        out = []
        while True:
            msg = self._parse_one()
            if msg is None:
                break
            out.append(msg)
        return out

    def _parse_one(self):
        buf = self._buf
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        if not isinstance(b0, int):  # py2 safety
            b0, b1 = ord(b0), ord(b1)
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        n = b1 & 0x7F
        idx = 2
        if n == 126:
            if len(buf) < 4:
                return None
            n = struct.unpack("!H", buf[2:4])[0]
            idx = 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n = struct.unpack("!Q", buf[2:10])[0]
            idx = 10
        mask = None
        if masked:
            mask = buf[idx:idx + 4]
            idx += 4
        if len(buf) < idx + n:
            return None
        payload = buf[idx:idx + n]
        self._buf = buf[idx + n:]
        if mask:
            payload = _apply_mask(payload, mask)
        if opcode == 0x9:  # ping
            self._sendall(self._frame(0xA, payload))
            return self._parse_one()
        if opcode == 0xA:  # pong
            return self._parse_one()
        if opcode == 0x8:
            raise ConnectionError("Server closed the session")
        if opcode in (0x1, 0x2, 0x0):
            self._fragments.append(payload)
            if not fin:
                return self._parse_one()
            data = b"".join(self._fragments)
            self._fragments = []
            return data.decode("utf-8")
        return self._parse_one()


class NetworkClient(object):
    """Keeps a connection alive with automatic reconnection.

    Engine-facing API (main thread): start(url), stop(), send(dict), poll() -> list of dicts.
    Status changes arrive in poll() as {"t": "_net", "state": ..., "detail": ...}.
    """

    def __init__(self, log=None):
        self._log = log or (lambda *a: None)
        self._inbox = queue.Queue()
        self._outbox = queue.Queue()
        self._thread = None
        self._stop = threading.Event()
        self._url = None
        self.connected = False

    def start(self, url):
        self.stop()
        self._url = url
        self._stop = threading.Event()
        self._outbox = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="LiveSyncNet", args=(self._stop, self._outbox))
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        if self._thread is not None:
            self._stop.set()
            self._thread = None
        self.connected = False

    def send(self, msg):
        self._outbox.put(json.dumps(msg, separators=(",", ":")))

    def poll(self, limit=500):
        out = []
        try:
            while len(out) < limit:
                out.append(self._inbox.get_nowait())
        except queue.Empty:
            pass
        return out

    def backlog(self):
        return self._outbox.qsize()

    def _status(self, state, detail="", attempt=0):
        self._inbox.put({"t": "_net", "state": state, "detail": detail, "attempt": attempt})

    def _run(self, stop, outbox):
        attempt = 0
        while not stop.is_set():
            conn = None
            try:
                self._status("connecting", attempt=attempt)
                conn = WebSocketConnection(self._url)
                attempt = 0
                self.connected = True
                # Messages queued while offline are stale (the engine resends what
                # matters after the server welcomes us), and hello must go first.
                try:
                    while True:
                        outbox.get_nowait()
                except queue.Empty:
                    pass
                self._status("open")
                last_ping = time.time()
                while not stop.is_set():
                    # flush outgoing
                    sent = 0
                    try:
                        while sent < 200:
                            conn.send_text(outbox.get_nowait())
                            sent += 1
                    except queue.Empty:
                        pass
                    if not conn.pending():
                        r, _, _ = select.select([conn], [], [], 0.02 if sent else 0.05)
                    else:
                        r = [conn]
                    if r:
                        for text in conn.read_available():
                            try:
                                self._inbox.put(json.loads(text))
                            except ValueError:
                                pass
                    if time.time() - last_ping > 10:
                        conn.send_ping()
                        last_ping = time.time()
            except Exception as e:  # network failures are expected; report softly
                self.connected = False
                if stop.is_set():
                    break
                attempt += 1
                self._log("network: %r" % (e,))
                self._status("down", detail=_friendly_error(e), attempt=attempt)
                delay = min(15.0, 0.75 * (2 ** min(attempt, 5))) * (0.8 + random.random() * 0.4)
                stop.wait(delay)
            finally:
                if conn is not None:
                    conn.close()
                self.connected = False
        self._status("closed")


def _friendly_error(e):
    text = str(e) or e.__class__.__name__
    if isinstance(e, HandshakeError):
        return text
    if isinstance(e, socket.gaierror):
        return "Can't find the server. Check the server address and your internet connection."
    if isinstance(e, (ConnectionRefusedError,)):
        return "The server isn't accepting connections right now."
    if isinstance(e, socket.timeout):
        return "The server took too long to answer."
    return "Connection lost (%s)." % text
