# -*- coding: utf-8 -*-
"""Audio file sharing: fingerprinting, uploading and downloading samples.

Recorded takes, bounced audio and imported samples live as files on one
computer. When an audio clip syncs, its file is identified by a SHA-1 hash,
uploaded once to the relay, and downloaded by the partner into
``~/Documents/LiveSync/Shared Audio``. Hashing and file I/O run on worker
threads so Live never stalls on a large WAV.
"""
from __future__ import absolute_import, print_function, unicode_literals

import base64
import hashlib
import json
import os
import threading

try:
    import queue
except ImportError:  # pragma: no cover
    import Queue as queue  # type: ignore

CHUNK = 262144


def default_root():
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    return os.path.join(docs if os.path.isdir(docs) else home, "LiveSync")


class FileStore(object):
    def __init__(self, root=None, log=None):
        self.root = root or default_root()
        self.shared_dir = os.path.join(self.root, "Shared Audio")
        try:
            os.makedirs(self.shared_dir)
        except OSError:
            pass
        self._log = log or (lambda *a: None)
        self._lock = threading.Lock()
        self._by_path = {}      # (path, size, mtime) -> info dict
        self._pending = set()
        self._path_by_hash = {}
        self._jobs = queue.Queue()
        self._done = queue.Queue()  # events for the engine (main thread)
        self._downloads = {}    # hash -> {"fh", "received", "total", "name", "tmp"}
        self._load_index()
        t = threading.Thread(target=self._worker, name="LiveSyncFiles")
        t.daemon = True
        t.start()

    # -- index ---------------------------------------------------------------
    def _index_path(self):
        return os.path.join(self.root, "file-index.json")

    def _load_index(self):
        try:
            with open(self._index_path()) as f:
                data = json.load(f)
            for h, p in data.items():
                if os.path.exists(p):
                    self._path_by_hash[h] = p
        except Exception:
            pass
        for name in os.listdir(self.shared_dir):
            if "-" in name and not name.endswith(".part"):
                self._path_by_hash.setdefault(name.split("-", 1)[0], os.path.join(self.shared_dir, name))

    def _save_index(self):
        try:
            with open(self._index_path(), "w") as f:
                json.dump(self._path_by_hash, f)
        except Exception:
            pass

    # -- fingerprinting --------------------------------------------------------
    def fingerprint(self, path):
        """Return {"hash","name","size"} for a file, or None while hashing."""
        if not path:
            return {"hash": "", "name": "", "size": 0}
        try:
            st = os.stat(path)
        except OSError:
            return {"hash": "", "name": os.path.basename(path), "size": 0, "missing": True}
        key = (path, st.st_size, int(st.st_mtime))
        with self._lock:
            info = self._by_path.get(key)
            if info is not None:
                return info
            if key not in self._pending:
                self._pending.add(key)
                self._jobs.put(("hash", key))
        return None

    def path_for(self, file_hash):
        p = self._path_by_hash.get(file_hash)
        if p and os.path.exists(p):
            return p
        return None

    def has(self, file_hash):
        return self.path_for(file_hash) is not None

    # -- uploading -------------------------------------------------------------
    def upload(self, file_hash, send):
        """Queue an upload; `send` is thread-safe (NetworkClient.send)."""
        self._jobs.put(("upload", (file_hash, send)))

    # -- downloading -----------------------------------------------------------
    def receive_chunk(self, msg):
        """Called on the main thread for each file_chunk. Returns (done, fraction)."""
        h = msg["hash"]
        if not all(c in "0123456789abcdef" for c in h):
            return True, 1.0
        dl = self._downloads.get(h)
        if dl is None:
            name = _safe_name(msg.get("name") or ("audio-%s.wav" % h[:8]))
            tmp = os.path.join(self.shared_dir, h + ".part")
            dl = {"fh": open(tmp, "wb"), "received": set(), "total": msg["total"], "tmp": tmp, "name": name,
                  "chunkSize": msg.get("chunkSize", CHUNK)}
            self._downloads[h] = dl
        data = base64.b64decode(msg["data"])
        dl["fh"].seek(msg["chunk"] * dl["chunkSize"])
        dl["fh"].write(data)
        dl["received"].add(msg["chunk"])
        if len(dl["received"]) >= dl["total"]:
            dl["fh"].close()
            del self._downloads[h]
            final = os.path.join(self.shared_dir, "%s-%s" % (h, dl["name"]))
            with open(dl["tmp"], "rb") as f:
                actual = _sha1_file(f)
            if actual != h:
                os.remove(dl["tmp"])
                return True, -1.0  # corrupt; engine will re-request
            if os.path.exists(final):
                os.remove(dl["tmp"])
            else:
                os.rename(dl["tmp"], final)
            self._path_by_hash[h] = final
            st = os.stat(final)
            with self._lock:
                self._by_path[(final, st.st_size, int(st.st_mtime))] = {"hash": h, "name": dl["name"], "size": st.st_size}
            self._save_index()
            return True, 1.0
        return False, len(dl["received"]) / float(dl["total"])

    def set_name_hint(self, file_hash, name):
        dl = self._downloads.get(file_hash)
        if dl is not None:
            dl["name"] = _safe_name(name)

    def poll_events(self):
        out = []
        try:
            while True:
                out.append(self._done.get_nowait())
        except queue.Empty:
            pass
        return out

    # -- worker ------------------------------------------------------------------
    def _worker(self):
        while True:
            kind, arg = self._jobs.get()
            try:
                if kind == "hash":
                    path, size, _ = arg
                    with open(path, "rb") as f:
                        h = _sha1_file(f)
                    info = {"hash": h, "name": _display_name(path, h), "size": size}
                    with self._lock:
                        self._by_path[arg] = info
                        self._pending.discard(arg)
                        self._path_by_hash.setdefault(h, path)
                    self._done.put({"t": "hashed", "hash": h, "path": path})
                elif kind == "upload":
                    file_hash, send = arg
                    path = self.path_for(file_hash)
                    if path is None:
                        continue
                    size = os.path.getsize(path)
                    total = max(1, (size + CHUNK - 1) // CHUNK)
                    name = os.path.basename(path)
                    if name.startswith(file_hash + "-"):
                        name = name[len(file_hash) + 1:]
                    with open(path, "rb") as f:
                        for i in range(total):
                            data = f.read(CHUNK)
                            send({"t": "file_put", "hash": file_hash, "chunk": i, "total": total, "size": size,
                                  "chunkSize": CHUNK, "name": name, "data": base64.b64encode(data).decode("ascii")})
                            self._done.put({"t": "upload_progress", "hash": file_hash, "fraction": (i + 1) / float(total), "name": name})
            except Exception as e:
                self._log("file worker: %r" % (e,))
                if kind == "hash":
                    with self._lock:
                        self._pending.discard(arg)
                        self._by_path[arg] = {"hash": "", "name": os.path.basename(arg[0]), "size": 0, "missing": True}


def _sha1_file(f):
    h = hashlib.sha1()
    while True:
        b = f.read(1 << 20)
        if not b:
            break
        h.update(b)
    return h.hexdigest()


def _display_name(path, file_hash):
    """Original file name, without the hash prefix we add to downloaded copies."""
    name = os.path.basename(path)
    if name.startswith(file_hash + "-"):
        name = name[len(file_hash) + 1:]
    return name


def _safe_name(name):
    name = os.path.basename(str(name))
    keep = "".join(c for c in name if c.isalnum() or c in " ._-()")
    return keep[:120] or "audio.wav"
