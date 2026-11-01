# -*- coding: utf-8 -*-
"""The LiveSync panel: a small local web server inside the Remote Script.

The panel page (panel.html) is shown inside Live by the LiveSync Max for Live
device (a [jweb] browser view), and can also be opened in any web browser at
http://127.0.0.1:47010 for Live editions without Max for Live.

The server binds to 127.0.0.1 only. Commands must carry an X-LiveSync header,
which ordinary web pages can't send cross-origin without a CORS preflight that
we never approve, so other websites can't drive your session.
"""
from __future__ import absolute_import, print_function, unicode_literals

import json
import os
import threading
import time

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
except ImportError:  # pragma: no cover
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer  # type: ignore
    from SocketServer import ThreadingMixIn  # type: ignore

try:
    import queue
except ImportError:  # pragma: no cover
    import Queue as queue  # type: ignore

HERE = os.path.dirname(os.path.abspath(__file__))


class Panel(object):
    """Holds the state the panel shows. Written on Live's main thread, read by HTTP threads."""

    def __init__(self, settings, show_message=None, port=47010, log=None):
        self.settings = settings
        self.port = port
        self._show_message = show_message or (lambda m: None)
        self._log = log or (lambda *a: None)
        self._lock = threading.Lock()
        self._commands = queue.Queue()
        self._state = {"status": "offline", "room": "", "host": False, "peers": [], "latency": -1, "backlog": 0}
        self._alert = None
        self._progress = None
        self._toasts = []
        self._toast_id = 0
        self._version = 0
        self._progress_label = ""
        self._httpd = None

    # -- engine-facing ---------------------------------------------------------
    def state(self, **kw):
        with self._lock:
            if any(self._state.get(k) != v for k, v in kw.items()):
                prev = self._state.get("status")
                self._state.update(kw)
                self._version += 1
                if prev != kw.get("status", prev):
                    self._announce(kw["status"])

    def alert(self, text, action=None):
        with self._lock:
            new = None if not text else {"text": text, "action": action or ""}
            if new != self._alert:
                self._alert = new
                self._version += 1
        if text:
            self._show_message("LiveSync: " + text)

    def progress(self, fraction, label=""):
        with self._lock:
            new = None if fraction is None else {"value": max(0.0, min(1.0, float(fraction))), "label": label or ""}
            self._progress_label = (label or "") if fraction is not None else ""
            if new != self._progress:
                self._progress = new
                self._version += 1

    def toast(self, text):
        with self._lock:
            self._toast_id += 1
            self._toasts.append({"id": self._toast_id, "text": text, "at": time.time()})
            self._toasts = self._toasts[-5:]
            self._version += 1

    def poll_commands(self):
        out = []
        try:
            while True:
                out.append(self._commands.get_nowait())
        except queue.Empty:
            pass
        return out

    def _announce(self, status):
        text = {
            "live": "connected",
            "syncing": "syncing with your partner…",
            "reconnecting": "connection dropped, reconnecting…",
            "out_of_sync": "out of sync, open the LiveSync panel and press Fix",
        }.get(status)
        if text:
            self._show_message("LiveSync: " + text)

    def snapshot(self):
        with self._lock:
            now = time.time()
            return {
                "v": self._version,
                "state": dict(self._state),
                "alert": self._alert,
                "progress": self._progress,
                "toasts": [t for t in self._toasts if now - t["at"] < 6],
                "fields": {
                    "server": self.settings.get("server", ""),
                    "name": self.settings.get("name", ""),
                    "room": self.settings.get("room", ""),
                },
                "options": dict((k, bool(self.settings.get(k))) for k in
                                ("sync_solo", "sync_arm", "sync_transport", "auto_resync")),
            }

    # -- http ----------------------------------------------------------------------
    def start(self):
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/state"):
                    self._send(200, json.dumps(panel.snapshot()))
                elif self.path in ("/", "/index.html", "/panel", "/device"):
                    try:
                        with open(os.path.join(HERE, "panel.html"), "rb") as f:
                            self._send(200, f.read(), "text/html; charset=utf-8")
                    except IOError:
                        self._send(500, "panel.html missing", "text/plain")
                else:
                    self._send(404, "{}")

            def do_POST(self):
                if self.path != "/cmd" or self.headers.get("X-LiveSync") != "1":
                    self._send(403, "{}")
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    cmd = json.loads(self.rfile.read(min(n, 65536)).decode("utf-8"))
                    panel._commands.put(cmd)
                    self._send(200, '{"ok":true}')
                except Exception:
                    self._send(400, "{}")

        class Server(ThreadingMixIn, HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        for port in (self.port, self.port + 1, self.port + 2):
            try:
                self._httpd = Server(("127.0.0.1", port), Handler)
                self.port = port
                break
            except Exception as e:
                self._log("panel port %d busy: %r" % (port, e))
        if self._httpd is None:
            return False
        t = threading.Thread(target=self._httpd.serve_forever, name="LiveSyncPanel")
        t.daemon = True
        t.start()
        return True

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
