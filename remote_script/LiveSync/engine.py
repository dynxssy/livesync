# -*- coding: utf-8 -*-
"""The sync engine: turns local edits into ops and partner ops into edits.

How it works, in one paragraph: the engine keeps a *shadow* copy of the set
(see model.py for the key layout). Every tick (~10x per second) it re-reads
the parts of the set that Live told us changed (listeners), plus a slice of
everything else (a background sweep that catches anything listeners miss).
Whatever differs from the shadow becomes an op, which goes to the relay. The
relay numbers every op and sends it to everyone, so both sets apply all edits
in the same order. Partner ops are written into Live and into the shadow, so
they don't echo back. When nobody is editing, both sides hash their shadow; a
mismatch means something drifted and the panel offers a one-click Resync,
which pulls a full snapshot from the host.

Nothing here raises into Live: every failure is turned into a friendly status
for the panel (ui.py) and logged to ~/Documents/LiveSync/livesync.log.
"""
from __future__ import absolute_import, print_function, unicode_literals

import collections
import hashlib
import json
import random
import time
import traceback

from . import model
from .model import MissingItem, Unsupported
from .settings import SESSION_OPTIONS

PROTOCOL_VERSION = 1
PENDING = object()
MISSING = object()

STRUCTURE_KEYS = ("cfg", "song", "list:returns", "list:scenes", "list:tracks")
SETTLE_SECONDS = 1.5


class Engine(object):
    def __init__(self, song, live_api, net, files, ui, settings, log, clock=time.time, browser_fn=None,
                 show_message=None):
        self.song = song
        self.api = live_api
        self.net = net
        self.files = files
        self.ui = ui
        self.settings = settings
        self.log = log
        self.clock = clock
        self.show_message = show_message or (lambda msg: None)
        self.loader = model.DeviceLoader(song, browser_fn or (lambda: None))

        self.shadow = {}
        self.uid_by_ptr = {}
        self.objs = {}
        self.order = {"tracks": [], "returns": [], "scenes": []}
        self._uid_counter = 0
        self._uid_prefix = "%04x" % random.randrange(0x10000)

        self.dirty = set()
        self.settle = {}
        self._sweep_keys = collections.deque()
        self._sweep_seq_start = None
        self._sweep_clean = True

        self.phase = "offline"      # offline | connecting | joining | live | reconnecting
        self.room = None
        self.peer = None
        self.is_host = False
        self.joined_at = None
        self.seq = None
        self.peers = []
        self.latency = None
        self._last_ping = 0
        self._want_create = False
        self._my_peer_ids = set()

        self.inbound = collections.deque()
        self.pending = {}           # key -> {field: count}
        self.my_ops = {}            # op id -> [(key, fields)]
        self._op_counter = 0
        self.parked = {}            # key -> op waiting for audio files
        self.waiting_files = {}     # hash -> set(keys)
        self.requested_files = set()
        self.uploaded = set()       # hashes the relay confirmed it has
        self.asked = set()          # hashes we've asked the relay about this connection
        self.shared = set()         # every hash our clips reference
        self.upload_queue = []
        self._file_names = {}
        self._snapshot_started = 0

        self.snapshot_job = None
        self.own_hashes = collections.OrderedDict()
        self._last_hash_sent = 0
        self.peer_hashes = {}
        self.mismatches = 0
        self.out_of_sync = False
        self.missing_items = set()
        self.unsupported = set()
        self.error_count = 0

        self.listeners = Listeners(self)
        self._refresh_structure()
        self._reseed_shadow()
        self._publish()

    # =====================================================================
    # Public actions (from the panel)
    # =====================================================================
    def host(self):
        self._want_create = True
        self.settings["room"] = ""
        self._connect()

    def join(self, room):
        self._want_create = False
        self.settings["room"] = _clean_code(room)
        if not self.settings["room"]:
            self.ui.alert("Type the session code your partner sent you, then press Join.", action=None)
            return
        self._connect()

    def leave(self):
        self.net.stop()
        self.phase = "offline"
        self.room = None
        self.peer = None
        self.seq = None
        self.peers = []
        self.inbound.clear()
        self.pending.clear()
        self.my_ops.clear()
        self.snapshot_job = None
        self.out_of_sync = False
        self.ui.alert(None)
        self.ui.progress(None)
        self._publish()
        self.show_message("LiveSync: left the session")

    def resync(self):
        if self.phase not in ("live", "joining"):
            if self.settings.get("room"):
                self._connect()
            return
        self.out_of_sync = False
        self.mismatches = 0
        self.error_count = 0
        self.ui.alert(None)
        if self.is_host:
            self.net.send({"t": "resync", "all": True})
            self.ui.toast("Sending your version of the set to everyone…")
        else:
            self.phase = "joining"
            self.ui.progress(0.0, "Requesting the latest version from the host…")
            self.net.send({"t": "resync"})
        self._publish()

    def dismiss(self):
        self.ui.alert(None)

    def set_option(self, key, value):
        if key in SESSION_OPTIONS:
            self.settings[key] = bool(value)
            self.settings.save()
            self.dirty.add("cfg")
            # Options change what gets synced; re-read everything once.
            self.dirty.update(self.all_keys())

    def set_fields(self, fields):
        for k in ("server", "name", "room"):
            if k in fields and fields[k] is not None:
                self.settings[k] = str(fields[k]).strip()[:200]
        self.settings.save()

    def handle_command(self, cmd):
        name = cmd.get("cmd")
        args = cmd.get("args") or {}
        if name == "host":
            self.host()
        elif name == "join":
            self.join(args.get("room") or self.settings.get("room"))
        elif name == "leave":
            self.leave()
        elif name in ("resync", "fix"):
            self.resync()
        elif name == "retry":
            self.ui.alert(None)
            self._connect()
        elif name == "dismiss":
            self.dismiss()
        elif name == "option":
            self.set_option(args.get("key"), args.get("value"))
        elif name == "fields":
            self.set_fields(args)

    def _connect(self):
        server = (self.settings.get("server") or "").strip()
        if not server:
            self.ui.alert("Set a server address first (ask your partner, or see the README).", action=None)
            return
        if "://" not in server:
            server = "wss://" + server
        self.phase = "connecting"
        self.seq = None if self._want_create or self.room != self.settings.get("room") else self.seq
        self.ui.alert(None)
        self.ui.progress(None)
        self.net.start(server)
        self._publish()
        self.settings.save()

    # =====================================================================
    # Tick
    # =====================================================================
    def tick(self):
        start = self.clock()
        try:
            self._handle_network()
            self._handle_file_events()
            if self.snapshot_job is not None:
                self._run_snapshot(start)
                return
            self._apply_inbound(start)
            self._refresh_structure()
            batch = self._scan(start)
            if batch and self.phase == "live":
                self._send_batch(batch)
            self._maybe_ping()
            self._flush_uploads()
        except Exception:
            self._internal_error("tick")
        finally:
            self._publish()

    # =====================================================================
    # Network messages
    # =====================================================================
    def _handle_network(self):
        for msg in self.net.poll():
            t = msg.get("t")
            try:
                handler = getattr(self, "_on_" + t, None) if t and t[0] != "_" else None
                if t == "_net":
                    self._on_net(msg)
                elif handler is not None:
                    handler(msg)
            except Exception:
                self._internal_error("message %s" % t)

    def _on_net(self, msg):
        state = msg["state"]
        if state == "open":
            hello = {"t": "hello", "v": PROTOCOL_VERSION, "name": self.settings.get("name") or "Producer",
                     "password": self.settings.get("password") or "", "room": self.settings.get("room") or "",
                     "create": self._want_create}
            if self.peer and self.room == self.settings.get("room"):
                hello.update({"peer": self.peer, "joinedAt": self.joined_at})
                if self.seq is not None:
                    hello["lastSeq"] = self.seq
            self.net.send(hello)
        elif state == "down":
            was_live = self.phase in ("live", "joining")
            self.phase = "reconnecting" if (was_live or self.phase == "reconnecting") else "connecting"
            attempt = msg.get("attempt", 1)
            if attempt >= 3:
                self.ui.alert("%s Still trying (attempt %d)…" % (msg.get("detail", ""), attempt), action="retry")
        elif state == "connecting" and self.phase == "offline":
            self.phase = "connecting"

    def _on_welcome(self, msg):
        first_time = self.room != msg["room"]
        self.room = msg["room"]
        self.settings["room"] = self.room
        self.settings.save()
        self.peer = msg["peer"]
        self._my_peer_ids.add(self.peer)
        self.is_host = msg["host"]
        self.joined_at = msg.get("joinedAt")
        self._want_create = False
        self.peers = msg.get("peers", [])
        self.ui.alert(None)
        # Anything queued before the drop was discarded by the network layer;
        # ask the relay about our audio again and resend unconfirmed edits later.
        self.asked.clear()
        self.upload_queue = [h for h in self.shared if h not in self.uploaded]
        if self.is_host:
            if self.seq is None:
                self.seq = msg["seq"]
            self.phase = "live"
            if not first_time:
                self._resend_unconfirmed()
            if first_time:
                self.show_message("LiveSync: session %s is open. Send this code to your partner." % self.room)
                self.ui.toast("Session open. Share the code %s with your partner." % self.room)
        else:
            self.phase = "joining"
            self.ui.progress(0.0, "Getting the project from your partner…")
        self._publish()

    def _on_caught_up(self, msg):
        self.phase = "live"
        self.ui.progress(None)
        self._resend_unconfirmed()

    def _resend_unconfirmed(self):
        # Edits sent right before a dropout that the relay never got.
        for op_id, (touched, batch) in list(self.my_ops.items()):
            self.net.send({"t": "op", "id": op_id, "op": {"b": batch}})

    def _on_peers(self, msg):
        before = set(p["id"] for p in self.peers)
        self.peers = msg.get("peers", [])
        was_host = self.is_host
        self.is_host = msg.get("host") == self.peer
        for p in self.peers:
            if p["id"] not in before and p["id"] != self.peer and before:
                self.ui.toast("%s joined the session" % p["name"])
                self.show_message("LiveSync: %s joined" % p["name"])
        if self.is_host and not was_host:
            self.ui.toast("You are now the session host.")

    def _on_snapshot_request(self, msg):
        if not self.is_host:
            return
        state = self.full_state(include_envelopes=True)
        self.net.send({"t": "snapshot", "for": msg.get("for"), "seq": self.seq, "state": state})

    def _on_snapshot_none(self, msg):
        self.phase = "live"
        self.ui.progress(None)

    def _on_snapshot(self, msg):
        self.inbound = collections.deque(e for e in self.inbound if e["seq"] > msg["seq"])
        self.pending.clear()
        self.my_ops.clear()
        self.parked.clear()
        self.phase = "joining"
        self.snapshot_job = self._snapshot_steps(msg["state"], msg["seq"])
        self._snapshot_progress = 0.0

    def _on_op(self, entry):
        if self.seq is not None and entry["seq"] <= self.seq and self.snapshot_job is None:
            return
        self.inbound.append(entry)

    def _on_hash(self, msg):
        self.peer_hashes[msg["seq"]] = msg["hash"]
        mine = self.own_hashes.get(msg["seq"])
        if mine is not None:
            self._compare_hash(mine, msg["hash"])

    def _on_pong(self, msg):
        self.latency = int(max(0, (self.clock() - msg["ts"]) * 1000))
        self.net.send({"t": "latency", "ms": self.latency})

    def _on_error(self, msg):
        code = msg.get("code")
        self.log("server error %s: %s" % (code, msg.get("message")))
        if code in ("no_room", "password", "full", "version"):
            self.net.stop()
            self.phase = "offline"
            self.room = None
            self.peer = None
            self.ui.alert(msg.get("message"), action=None)
        elif code in ("file_corrupt",):
            self.uploaded.clear()
        else:
            self.ui.toast(msg.get("message") or "The server had a problem.")

    def _on_chat(self, msg):
        if msg.get("from") != self.peer:
            self.ui.toast("%s: %s" % (msg.get("name"), msg.get("text")))

    # -- files -----------------------------------------------------------
    def _on_file_has(self, msg):
        if msg.get("present"):
            self.uploaded.add(msg["hash"])
        else:
            self.files.upload(msg["hash"], self.net.send)

    def _on_file_done(self, msg):
        self.uploaded.add(msg["hash"])
        self.ui.progress(None)

    def _on_file_available(self, msg):
        h = msg["hash"]
        if h in self.waiting_files and not self.files.has(h):
            self.net.send({"t": "file_get", "hash": h})

    def _on_file_missing(self, msg):
        # The partner hasn't finished uploading yet; file_available will arrive.
        pass

    def _on_file_chunk(self, msg):
        h = msg["hash"]
        msg.setdefault("name", self._file_names.get(h))
        done, frac = self.files.receive_chunk(msg)
        if frac < 0:
            self.net.send({"t": "file_get", "hash": h})
            return
        if not done:
            self.ui.progress(frac, "Downloading audio from your partner… %d%%" % int(frac * 100))
            return
        self.ui.progress(None)
        self.requested_files.discard(h)
        keys = self.waiting_files.pop(h, set())
        for key in keys:
            op = self.parked.pop(key, None)
            if op is not None:
                self._apply_one(op)

    def _handle_file_events(self):
        for ev in self.files.poll_events():
            if ev["t"] == "hashed":
                self.dirty.add("__rescan_audio__")
            elif ev["t"] == "upload_progress" and ev["fraction"] < 1.0:
                self.ui.progress(ev["fraction"], "Sharing %s… %d%%" % (ev["name"], int(ev["fraction"] * 100)))

    def _need_file(self, info, key, op):
        h = info.get("hash")
        if not h or self.files.has(h):
            return False
        self.parked[key] = op
        self.waiting_files.setdefault(h, set()).add(key)
        self._file_names[h] = info.get("name")
        if h not in self.requested_files:
            self.requested_files.add(h)
            self.net.send({"t": "file_get", "hash": h})
            self.ui.progress(0.0, "Downloading %s…" % (info.get("name") or "audio"))
        return True

    def _share_files(self, value):
        for info in _iter_files(value):
            h = info.get("hash")
            if h:
                self.shared.add(h)
                if h not in self.uploaded and h not in self.upload_queue:
                    self.upload_queue.append(h)

    def _flush_uploads(self):
        while self.upload_queue and self.phase == "live":
            h = self.upload_queue.pop(0)
            if h not in self.asked and self.files.has(h):
                self.asked.add(h)
                self.net.send({"t": "file_has", "hash": h})

    # =====================================================================
    # Applying partner ops
    # =====================================================================
    def _apply_inbound(self, start):
        while self.inbound and self.clock() - start < 0.04:
            entry = self.inbound.popleft()
            if self.seq is not None and entry["seq"] <= self.seq:
                continue
            self.seq = entry["seq"]
            if entry.get("from") in self._my_peer_ids:
                self._ack(entry.get("id"))
                continue
            for op in entry["op"].get("b", []):
                op = self._protect(op)
                if op is not None:
                    self._apply_one(op)
        if len(self.inbound) > 50:
            self.ui.progress(0.5, "Catching up with %d changes…" % len(self.inbound))
        elif self.phase == "live" and not self.inbound and not self.requested_files and getattr(self.ui, "_progress_label", "").startswith("Catching"):
            self.ui.progress(None)

    def _protect(self, op):
        """Drop parts of a partner op that our own not-yet-confirmed edits will override.

        The relay orders our pending op *after* this one, so on every machine the
        final value is ours. Applying theirs here would make us diverge."""
        pend = self.pending.get(op["k"])
        if not pend:
            return op
        if "*" in pend:
            return None
        if "m" in op:
            m = dict((k, v) for k, v in op["m"].items() if k not in pend)
            return dict(op, m=m) if m else None
        if "x" in op:
            x = dict((k, v) for k, v in op["x"].items() if str(k) not in pend)
            return dict(op, x=x) if x else None
        if "v" in op:
            mine = self.shadow.get(op["k"])
            v = op["v"]
            if isinstance(v, dict) and isinstance(mine, dict):
                v = dict(v)
                for f in pend:
                    if f in mine:
                        v[f] = mine[f]
                return dict(op, v=v)
            if isinstance(v, list) and isinstance(mine, list) and len(v) == len(mine):
                v = list(v)
                for f in pend:
                    v[int(f)] = mine[int(f)]
                return dict(op, v=v)
        return op

    def _apply_one(self, op):
        key = op["k"]
        try:
            self._apply_key(key, op)
        except MissingItem as e:
            self._note_missing(str(e))
        except Unsupported as e:
            self._note_unsupported(str(e))
        except Exception:
            self._internal_error("apply %s" % key, sync_problem=True)
        # Re-read what Live actually did so it doesn't echo back.
        self._silent_read(key)
        if key.startswith("d:"):
            self.settle[key] = self.clock() + SETTLE_SECONDS

    def _silent_read(self, key):
        try:
            cur = self._read(key)
        except Exception:
            cur = PENDING
        if cur is MISSING:
            self.shadow.pop(key, None)
        elif cur is not PENDING:
            self.shadow[key] = cur

    def _value(self, key, op):
        if "v" in op:
            return op["v"]
        base = self.shadow.get(key)
        if "m" in op:
            out = dict(base or {})
            out.update(op["m"])
            return out
        if "x" in op:
            out = list(base or [])
            for i, v in op["x"].items():
                i = int(i)
                if i < len(out):
                    out[i] = v
            return out
        return None

    def _apply_key(self, key, op):
        kind, _, rest = key.partition(":")
        if key == "song":
            model.write_song(self.song, op.get("m") or op.get("v") or {})
            return
        if key == "cfg":
            changed = op.get("m") or op.get("v") or {}
            for k, v in changed.items():
                if k in SESSION_OPTIONS:
                    self.settings[k] = bool(v)
            self.settings.save()
            self.dirty.update(self.all_keys())
            self.ui.toast("Your partner changed the sync options.")
            return
        if kind == "list":
            self._apply_list(rest, op["v"], adopt=op.get("adopt", False))
            return
        parts = rest.split(":")
        obj = self.objs.get(parts[0])
        if obj is None:
            raise KeyError("unknown object %s" % parts[0])
        if kind == "t":
            model.write_track(obj, op.get("m") or op.get("v") or {})
        elif kind == "s":
            model.write_scene(obj, op.get("m") or op.get("v") or {})
        elif kind == "d":
            missing = model.write_device_chain(obj, op["v"], self.loader)
            for m in missing:
                self._note_missing(m)
            self.listeners.bind_track(parts[0], obj)
        elif kind == "p":
            devices = model.syncable_devices(obj)
            idx = int(parts[1])
            if idx >= len(devices):
                return  # device didn't load here (missing plugin); already reported
            values = op["x"] if "x" in op else op.get("v") or []
            model.write_params(devices[idx], values)
        elif kind == "c":
            self._apply_clip(key, obj, parts[1], op)
        elif kind == "a":
            value = self._value(key, op)
            for info in _iter_files(value):
                if self._need_file(info, key, op):
                    return
            model.write_arrangement(obj, value, self.files, self.api)
        elif kind == "e":
            slot = self._slot(obj, parts[1])
            if slot is not None and slot.has_clip:
                model.write_envelopes(slot.clip, obj, op.get("v") or {})

    def _slot(self, track, scene_uid):
        try:
            idx = self.order["scenes"].index(scene_uid)
            return list(track.clip_slots)[idx]
        except (ValueError, IndexError):
            return None

    def _apply_clip(self, key, track, scene_uid, op):
        slot = self._slot(track, scene_uid)
        if slot is None:
            raise KeyError("no clip slot for %s" % key)
        value = self._value(key, op)
        if value is None:
            if slot.has_clip:
                slot.delete_clip()
            return
        if not value.get("midi") and self._need_file(value.get("file") or {}, key, dict(op, v=value)):
            return
        if not slot.has_clip or bool(slot.clip.is_midi_clip) != bool(value.get("midi")) or self._file_changed(slot.clip, value):
            clip = model.create_session_clip(slot, value, self.files)
            model.write_clip_props(clip, value, self.api)
            self.listeners.bind_clip(track, slot)
        else:
            model.write_clip_props(slot.clip, op.get("m") or value, self.api)

    def _file_changed(self, clip, value):
        if clip.is_midi_clip:
            return False
        info = self.files.fingerprint(clip.file_path) or {}
        return info.get("hash") != (value.get("file") or {}).get("hash")

    def _apply_list(self, name, desired, adopt=False):
        """Make song.tracks / return_tracks / scenes match `desired` [[uid, kind], ...]."""
        live_list = self._live_list(name)
        want_uids = [u for u, _ in desired]
        kinds = dict((u, k) for u, k in desired)
        if adopt:
            # Joining: reuse whatever is already in this set, position by position.
            i = 0
            while i < len(desired):
                live_list = self._live_list(name)
                uid, kind = desired[i]
                if i < len(live_list) and (name == "scenes" or _kind_ok(live_list[i], kind)):
                    self._bind(live_list[i], uid)
                else:
                    self._create(name, i, kind)
                    self._bind(self._live_list(name)[i], uid)
                i += 1
            live_list = self._live_list(name)
            for j in range(len(live_list) - 1, len(desired) - 1, -1):
                self._delete(name, j)
            self._refresh_structure()
            return
        # Deletions first (highest index first so indices stay valid)
        for j in range(len(live_list) - 1, -1, -1):
            uid = self.uid_by_ptr.get(_ptr(live_list[j]))
            if uid not in kinds:
                self._delete(name, j)
        # Insertions in order
        for i, uid in enumerate(want_uids):
            live_list = self._live_list(name)
            cur_uids = [self.uid_by_ptr.get(_ptr(o)) for o in live_list]
            if i < len(cur_uids) and cur_uids[i] == uid:
                continue
            if uid in cur_uids:
                # Tracks can't be reordered through Live's API.
                self._note_unsupported("moving tracks or scenes")
                continue
            self._create(name, min(i, len(live_list)), kinds[uid])
            self._bind(self._live_list(name)[min(i, len(live_list))], uid)
        self._refresh_structure()

    def _live_list(self, name):
        if name == "tracks":
            return list(self.song.tracks)
        if name == "returns":
            return list(self.song.return_tracks)
        return list(self.song.scenes)

    def _create(self, name, index, kind):
        if name == "tracks":
            if kind == "group":
                self._note_unsupported("creating group tracks (an audio track was added instead)")
            if kind == "midi":
                self.song.create_midi_track(index)
            else:
                self.song.create_audio_track(index)
        elif name == "returns":
            self.song.create_return_track()
        else:
            self.song.create_scene(index)

    def _delete(self, name, index):
        if name == "tracks":
            if len(self.song.tracks) <= 1:
                # Live needs at least one track: add a fresh one so we can remove the old.
                self.song.create_audio_track(-1)
            self.song.delete_track(index)
        elif name == "returns":
            self.song.delete_return_track(index)
        else:
            if len(self.song.scenes) <= 1:
                self.song.create_scene(-1)
            self.song.delete_scene(index)

    def _bind(self, obj, uid):
        old = self.uid_by_ptr.get(_ptr(obj))
        if old and old != uid:
            self.objs.pop(old, None)
        self.uid_by_ptr[_ptr(obj)] = uid
        self.objs[uid] = obj

    # =====================================================================
    # Reading local state
    # =====================================================================
    def _new_uid(self):
        self._uid_counter += 1
        return "%s%x" % (self._uid_prefix, self._uid_counter)

    def _refresh_structure(self):
        lists = {"tracks": list(self.song.tracks), "returns": list(self.song.return_tracks),
                 "scenes": list(self.song.scenes)}
        seen = {}
        changed = False
        for name, objs in lists.items():
            uids = []
            for o in objs:
                p = _ptr(o)
                uid = self.uid_by_ptr.get(p)
                if uid is None:
                    uid = self._new_uid()
                    self.uid_by_ptr[p] = uid
                uids.append(uid)
                seen[p] = uid
                self.objs[uid] = o
            if uids != self.order[name]:  # noqa
                changed = True
                self.order[name] = uids
        master = self.song.master_track
        self.uid_by_ptr[_ptr(master)] = "master"
        seen[_ptr(master)] = "master"
        self.objs["master"] = master
        if len(seen) != len(self.uid_by_ptr):
            for p in [p for p in self.uid_by_ptr if p not in seen]:
                uid = self.uid_by_ptr.pop(p)
                self.objs.pop(uid, None)
                self.listeners.unbind_group(uid)
            alive = set(seen.values())
            for k in [k for k in self.shadow if _key_uid(k) and _key_uid(k) not in alive]:
                del self.shadow[k]
            changed = True
        if changed:
            alive_groups = set(self.order["tracks"] + self.order["returns"] + ["master", "song"])
            for g in [g for g in self.listeners.groups if g not in alive_groups]:
                self.listeners.unbind_group(g)
            for uid in self.order["tracks"] + self.order["returns"] + ["master"]:
                if not self.listeners.has_group(uid):
                    self.listeners.bind_track(uid, self.objs[uid])
                    self.dirty.update(self._track_keys(uid))
            self.listeners.bind_song()
            self.dirty.update(STRUCTURE_KEYS)
            for uid in self.order["scenes"]:
                self.dirty.add("s:" + uid)
            # Clip slots are addressed by scene; a scene change re-reads all clips.
            for uid in self.order["tracks"]:
                self.dirty.update(self._clip_keys(uid))

    def _track_keys(self, uid):
        keys = ["t:" + uid, "d:" + uid]
        track = self.objs.get(uid)
        if track is not None:
            keys += ["p:%s:%d" % (uid, i) for i in range(len(model.syncable_devices(track)))]
            if uid in self.order["tracks"]:
                keys += self._clip_keys(uid)
                keys.append("a:" + uid)
        return keys

    def _clip_keys(self, uid):
        return ["c:%s:%s" % (uid, s) for s in self.order["scenes"]]

    def all_keys(self):
        keys = list(STRUCTURE_KEYS)
        keys += ["s:" + u for u in self.order["scenes"]]
        for uid in self.order["returns"] + self.order["tracks"] + ["master"]:
            keys += self._track_keys(uid)
        return keys

    def _read(self, key):
        """Serialize one entity. Returns PENDING if not ready, MISSING if gone."""
        if key == "song":
            return model.read_song(self.song, self.settings)
        if key == "cfg":
            return dict((k, bool(self.settings.get(k))) for k in SESSION_OPTIONS)
        kind, _, rest = key.partition(":")
        if kind == "list":
            if rest == "scenes":
                return [[u, None] for u in self.order["scenes"]]
            out = []
            for o in self._live_list(rest):
                uid = self.uid_by_ptr.get(_ptr(o))
                if uid is None:
                    return PENDING
                out.append([uid, model.track_kind(o)])
            return out
        parts = rest.split(":")
        obj = self.objs.get(parts[0])
        if obj is None:
            return MISSING
        if kind == "t":
            return model.read_track(obj, self.settings, is_master=parts[0] == "master")
        if kind == "s":
            return model.read_scene(obj)
        if kind == "d":
            return model.read_device_chain(obj)
        if kind == "p":
            devices = model.syncable_devices(obj)
            idx = int(parts[1])
            return model.read_params(devices[idx]) if idx < len(devices) else MISSING
        if kind == "c":
            slot = self._slot(obj, parts[1])
            if slot is None:
                return MISSING
            if not slot.has_clip:
                return None
            clip = slot.clip
            if getattr(clip, "is_recording", False):
                return PENDING
            st = model.read_clip(clip, self.files)
            return PENDING if st is None else st
        if kind == "a":
            st = model.read_arrangement(obj, self.files)
            return PENDING if st is None else st
        if kind == "e":
            slot = self._slot(obj, parts[1])
            if slot is None or not slot.has_clip:
                return MISSING
            return model.read_envelopes(slot.clip, obj)
        return MISSING

    def _reseed_shadow(self):
        self.shadow = {}
        for key in self.all_keys():
            self._silent_read(key)
        self.dirty.clear()

    def full_state(self, include_envelopes=False):
        state = {}
        for key in self.all_keys():
            v = self.shadow.get(key, MISSING)
            if v is MISSING:
                v = self._read(key)
            if v is not MISSING and v is not PENDING:
                state[key] = v
            if include_envelopes and key.startswith("c:") and v:
                env = self._read("e:" + key[2:])
                if env:
                    state["e:" + key[2:]] = env
        return state

    # =====================================================================
    # Detecting local changes
    # =====================================================================
    def _scan(self, start):
        batch = []
        now = self.clock()
        if "__rescan_audio__" in self.dirty:
            self.dirty.discard("__rescan_audio__")
            self.dirty.update(k for k in self.all_keys() if k[:2] in ("c:", "a:"))
        for key in STRUCTURE_KEYS:
            self._scan_key(key, batch, now)
        # Envelopes are only polled on the clip you're looking at.
        detail = self._detail_clip_key()
        if detail and int(now * 2) % 4 == 0:
            self._scan_key(detail, batch, now)
        while self.dirty and self.clock() - start < 0.03:
            self._scan_key(self.dirty.pop(), batch, now)
        if not self._sweep_keys:
            self._end_sweep()
            self._sweep_keys.extend(self.all_keys())
            self._sweep_seq_start = self.seq
            self._sweep_clean = True
        budget_end = self.clock() + 0.008
        while self._sweep_keys and self.clock() < budget_end:
            if self._scan_key(self._sweep_keys.popleft(), batch, now):
                self._sweep_clean = False
        if batch:
            self._sweep_clean = False
        return batch

    def _detail_clip_key(self):
        try:
            clip = self.song.view.detail_clip
            if clip is None or not hasattr(clip, "canonical_parent"):
                return None
            slot = clip.canonical_parent
            track = slot.canonical_parent
            uid = self.uid_by_ptr.get(_ptr(track))
            idx = list(track.clip_slots).index(slot)
            return "e:%s:%s" % (uid, self.order["scenes"][idx])
        except Exception:
            return None

    def _scan_key(self, key, batch, now):
        try:
            cur = self._read(key)
        except Exception:
            self._internal_error("read %s" % key)
            return False
        if cur is PENDING:
            return False
        old = self.shadow.get(key, MISSING)
        if cur is MISSING:
            self.shadow.pop(key, None)
            return False
        if cur == old:
            return False
        self.shadow[key] = cur
        if self._settling(key, now) or self.phase != "live":
            return False
        op = _make_op(key, old, cur)
        batch.append(op)
        if key.startswith("d:"):
            uid = key[2:]
            self.listeners.bind_track(uid, self.objs.get(uid))
            self.dirty.update(k for k in self._track_keys(uid) if k.startswith("p:"))
        if key[:2] in ("c:", "a:"):
            self._share_files(cur)
        return True

    def _settling(self, key, now):
        if key.startswith("p:"):
            key = "d:" + key.split(":")[1]
        until = self.settle.get(key)
        if until is None:
            return False
        if now >= until:
            del self.settle[key]
            return False
        return True

    def _send_batch(self, batch):
        self._op_counter += 1
        op_id = "%s-%d" % (self.peer, self._op_counter)
        touched = []
        for op in batch:
            fields = list(op["m"].keys()) if "m" in op else ([str(i) for i in op["x"]] if "x" in op else ["*"])
            touched.append((op["k"], fields))
            pend = self.pending.setdefault(op["k"], {})
            for f in fields:
                pend[f] = pend.get(f, 0) + 1
        self.my_ops[op_id] = (touched, batch)
        self.net.send({"t": "op", "id": op_id, "op": {"b": batch}})

    def _ack(self, op_id):
        for key, fields in self.my_ops.pop(op_id, ([], []))[0]:
            pend = self.pending.get(key)
            if not pend:
                continue
            for f in fields:
                n = pend.get(f, 0) - 1
                if n <= 0:
                    pend.pop(f, None)
                else:
                    pend[f] = n
            if not pend:
                del self.pending[key]

    # =====================================================================
    # Snapshots (joining and resyncing)
    # =====================================================================
    def _snapshot_steps(self, state, seq):
        order = list(STRUCTURE_KEYS)
        rank = {"s": 0, "t": 1, "d": 2, "p": 3, "c": 4, "a": 5, "e": 6}
        rest = sorted((k for k in state if k not in order), key=lambda k: (rank.get(k[0], 9), k))
        keys = [k for k in order if k in state] + rest
        # Start every audio download up front so they run while we build tracks.
        for k in keys:
            for info in _iter_files(state[k]):
                h = info.get("hash")
                if h and not self.files.has(h) and h not in self.requested_files:
                    self.requested_files.add(h)
                    self._file_names[h] = info.get("name")
                    self.net.send({"t": "file_get", "hash": h})
        total = float(len(keys)) or 1.0
        for i, key in enumerate(keys):
            value = state[key]
            if key.startswith("list:"):
                self._apply_one({"k": key, "v": value, "adopt": True})
            else:
                cur = self._read(key)
                if cur is PENDING or cur != value:
                    self._apply_one({"k": key, "v": value})
            yield (i + 1) / total, _describe(key, value)
        # Remove local clips/devices the host doesn't have (keys absent in state).
        self._refresh_structure()
        for key in self.all_keys():
            if key.startswith("c:") and key not in state and self._read(key) not in (None, PENDING, MISSING):
                self._apply_one({"k": key, "v": None})
        self.seq = seq
        waited = self.clock()
        while self.parked and self.clock() - waited < 180:
            yield 1.0, "Waiting for audio files from your partner…"
        if self.parked:
            names = sorted(set(self._file_names.get(h) or "audio" for h in self.waiting_files))
            self.ui.alert("Some audio didn't arrive: %s." % ", ".join(names), action="resync")
        self._reseed_shadow()
        self.settle.clear()

    def _run_snapshot(self, start):
        label = None
        try:
            while self.clock() - start < 0.05:
                frac, label = next(self.snapshot_job)
                self._snapshot_progress = frac
        except StopIteration:
            self.snapshot_job = None
            self.phase = "live"
            self.out_of_sync = False
            self.mismatches = 0
            self.own_hashes.clear()
            self.peer_hashes.clear()
            self.ui.progress(None)
            self._sweep_keys.clear()
            if self.missing_items:
                self.ui.alert("Synced, but these aren't installed on this computer: %s." % ", ".join(sorted(self.missing_items)),
                              action="dismiss")
            else:
                self.ui.toast("You're in sync with your partner.")
            self.show_message("LiveSync: in sync")
            return
        except Exception:
            self.snapshot_job = None
            self.phase = "live"
            self._internal_error("snapshot", sync_problem=True)
            return
        if label:
            self.ui.progress(self._snapshot_progress, label)

    # =====================================================================
    # Drift detection
    # =====================================================================
    def state_hash(self):
        items = [(k, v) for k, v in self.shadow.items() if not k.startswith("e:")]
        items.sort()
        blob = json.dumps(items, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]

    def _quiet(self):
        return (self.phase == "live" and not self.pending and not self.inbound and not self.parked
                and not self.settle and self.snapshot_job is None)

    def _end_sweep(self):
        if not (self._sweep_clean and self._quiet() and self.seq is not None and self.seq == self._sweep_seq_start):
            return
        now = self.clock()
        if self.seq in self.own_hashes and now - self._last_hash_sent < 4.0:
            return
        self._last_hash_sent = now
        h = self.state_hash()
        self.own_hashes[self.seq] = h
        while len(self.own_hashes) > 50:
            self.own_hashes.popitem(last=False)
        self.net.send({"t": "hash", "seq": self.seq, "hash": h})
        theirs = self.peer_hashes.get(self.seq)
        if theirs is not None:
            self._compare_hash(h, theirs)

    def _compare_hash(self, mine, theirs):
        if mine == theirs:
            if self.out_of_sync and not self.missing_items:
                self.ui.alert(None)
            self.mismatches = 0
            self.out_of_sync = False
            return
        self.mismatches += 1
        if self.mismatches >= 2 and not self.out_of_sync:
            self.out_of_sync = True
            if self.settings.get("auto_resync") and not self.is_host:
                self.resync()
            else:
                self.ui.alert("Your set and your partner's set have drifted apart.", action="resync")

    # =====================================================================
    # Problems → friendly UI
    # =====================================================================
    def _note_missing(self, name):
        if name not in self.missing_items:
            self.missing_items.add(name)
            self.ui.alert("\u201c%s\u201d isn't installed on this computer, so it couldn't be added." % name, action="dismiss")
            self.log("missing: %s" % name)

    def _note_unsupported(self, what):
        if what not in self.unsupported:
            self.unsupported.add(what)
            self.ui.toast("Your partner did something Live can't sync remotely: %s." % what)
            self.log("unsupported: %s" % what)

    def _internal_error(self, where, sync_problem=False):
        self.error_count += 1
        self.log("error in %s:\n%s" % (where, traceback.format_exc()))
        if sync_problem and self.error_count >= 1:
            self.ui.alert("A change didn't come through cleanly.", action="resync")

    def _maybe_ping(self):
        now = self.clock()
        if self.phase in ("live", "joining") and now - self._last_ping > 2.0:
            self._last_ping = now
            self.net.send({"t": "ping", "ts": now})

    def _publish(self):
        if self.snapshot_job is not None or self.phase == "joining":
            status = "syncing"
        elif self.phase == "live" and self.out_of_sync:
            status = "out_of_sync"
        else:
            status = self.phase
        others = [p["name"] for p in self.peers if p.get("id") != self.peer]
        self.ui.state(status=status, room=self.room or "", host=self.is_host, peers=others,
                      latency=self.latency if self.latency is not None else -1,
                      backlog=len(self.inbound) + self.net.backlog())

    def shutdown(self):
        self.listeners.unbind_all()
        self.net.stop()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ptr(obj):
    p = getattr(obj, "_live_ptr", None)
    return p if p is not None else id(obj)


def _key_uid(key):
    if key in STRUCTURE_KEYS or key.startswith("list:"):
        return None
    parts = key.split(":")
    return parts[1] if len(parts) > 1 else None


def _kind_ok(track, kind):
    k = model.track_kind(track)
    return k == kind or (kind == "group" and k == "audio")


def _clean_code(code):
    return "".join(c for c in str(code or "").upper() if c.isalnum())


def _make_op(key, old, cur):
    if isinstance(old, dict) and isinstance(cur, dict):
        identity_changed = key.startswith("c:") and (
            old.get("midi") != cur.get("midi") or (old.get("file") or {}).get("hash") != (cur.get("file") or {}).get("hash"))
        if not identity_changed:
            changed = dict((k, v) for k, v in cur.items() if old.get(k, MISSING) != v)
            if changed:
                return {"k": key, "m": changed}
    if (key.startswith("p:") and isinstance(old, list) and isinstance(cur, list) and len(old) == len(cur)):
        x = dict((str(i), v) for i, (a, v) in enumerate(zip(old, cur)) if a != v)
        if len(x) <= max(4, len(cur) // 3):
            return {"k": key, "x": x}
    return {"k": key, "v": cur}


def _iter_files(value):
    if isinstance(value, dict):
        f = value.get("file")
        if isinstance(f, dict) and f.get("hash"):
            yield f
    elif isinstance(value, list):
        for v in value:
            if isinstance(v, dict):
                f = v.get("file")
                if isinstance(f, dict) and f.get("hash"):
                    yield f


def _describe(key, value):
    kind = key.split(":")[0]
    if kind == "t" and isinstance(value, dict):
        return "Setting up track \u201c%s\u201d" % value.get("name", "")
    if kind == "d":
        return "Loading devices and plugins"
    if kind == "p":
        return "Dialing in device settings"
    if kind == "c":
        return "Copying clips and notes"
    if kind == "a":
        return "Building the arrangement"
    if kind == "e":
        return "Copying clip automation"
    if kind == "list":
        return "Creating tracks and scenes"
    return "Syncing song settings"


class Listeners(object):
    """Live listeners that mark keys dirty so edits go out within ~100 ms.

    They never touch the set themselves (Live forbids changes from inside
    notifications); the background sweep is the safety net if one is missed."""

    def __init__(self, engine):
        self.e = engine
        self.groups = {}

    def has_group(self, group):
        return group in self.groups

    def _add(self, group, obj, prop, fn):
        adder = getattr(obj, "add_%s_listener" % prop, None)
        if adder is None:
            return
        try:
            adder(fn)
            self.groups.setdefault(group, []).append((obj, prop, fn))
        except Exception:
            pass

    def unbind_group(self, group):
        for obj, prop, fn in self.groups.pop(group, []):
            try:
                if getattr(obj, "%s_has_listener" % prop)(fn):
                    getattr(obj, "remove_%s_listener" % prop)(fn)
            except Exception:
                pass

    def unbind_all(self):
        for g in list(self.groups):
            self.unbind_group(g)

    def bind_song(self):
        if "song" in self.groups:
            return
        song = self.e.song
        mark = lambda: self.e.dirty.add("song")
        for prop in ("tempo", "signature_numerator", "signature_denominator", "root_note", "scale_name",
                     "swing_amount", "groove_amount", "loop", "loop_start", "loop_length", "is_playing"):
            self._add("song", song, prop, mark)

    def bind_track(self, uid, track):
        if track is None:
            return
        self.unbind_group(uid)
        dirty = self.e.dirty
        tkey = "t:" + uid
        mark_t = lambda: dirty.add(tkey)
        for prop in ("name", "color", "mute", "solo", "arm", "current_monitoring_state"):
            self._add(uid, track, prop, mark_t)
        mixer = track.mixer_device
        for param in [mixer.volume, mixer.panning] + list(mixer.sends):
            self._add(uid, param, "value", mark_t)
        self._add(uid, track, "devices", lambda: dirty.add("d:" + uid))
        for i, dev in enumerate(model.syncable_devices(track)):
            pkey = "p:%s:%d" % (uid, i)
            for p in dev.parameters:
                self._add(uid, p, "value", (lambda k: lambda: dirty.add(k))(pkey))
        if hasattr(track, "arrangement_clips"):
            self._add(uid, track, "arrangement_clips", lambda: dirty.add("a:" + uid))
        for slot in getattr(track, "clip_slots", []):
            self.bind_clip(track, slot, group=uid)

    def bind_clip(self, track, slot, group=None):
        engine = self.e
        uid = engine.uid_by_ptr.get(_ptr(track))
        if uid is None:
            return
        group = group or uid

        def mark():
            try:
                idx = list(track.clip_slots).index(slot)
                engine.dirty.add("c:%s:%s" % (uid, engine.order["scenes"][idx]))
            except Exception:
                pass

        def on_has_clip():
            mark()
            if slot.has_clip:
                self._bind_clip_props(group, slot.clip, mark)

        self._add(group, slot, "has_clip", on_has_clip)
        if slot.has_clip:
            self._bind_clip_props(group, slot.clip, mark)

    def _bind_clip_props(self, group, clip, mark):
        for prop in ("notes", "name", "color", "loop_start", "loop_end", "start_marker", "end_marker", "looping",
                     "gain", "pitch_coarse", "pitch_fine", "warping", "warp_mode", "muted"):
            self._add(group, clip, prop, mark)
