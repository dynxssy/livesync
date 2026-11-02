# -*- coding: utf-8 -*-
"""Reading and writing the Live set as a flat map of plain-JSON "entities".

Every syncable thing in a set is addressed by a string key:

    song                      tempo, time signature, key/scale, groove, loop...
    list:tracks               [[uid, kind], ...]   order + type of normal tracks
    list:returns              [[uid, kind], ...]   return tracks
    list:scenes               [[uid, null], ...]
    t:<track>                 name, colour, mute, solo, volume, pan, sends...
    s:<scene>                 scene name, colour, tempo
    d:<track>                 device chain: [{cls, name, kind}, ...]
    p:<track>:<i>             parameter values of device i (list of floats)
    c:<track>:<scene>         session clip in that slot (or None)
    a:<track>                 arrangement clips on that track
    e:<track>:<scene>         clip envelopes (automation) of a session clip

Values are rounded on the way out so that both computers produce identical
JSON for identical sets; that is what makes the cheap hash comparison work.

The functions here only use attributes that exist in the Live Object Model
(Live 11.1+, with extra features gated behind hasattr checks for Live 12).
"""
from __future__ import absolute_import, print_function, unicode_literals

import os

NOTE_SPAN = 1 << 14  # beats; large enough to cover any clip

# Devices we never sync (our own UI device lives on a track).
IGNORED_DEVICE_PREFIXES = ("LiveSync",)


def r4(x):
    try:
        return round(float(x), 4)
    except Exception:
        return 0.0


def r6(x):
    return round(float(x), 6)


def has(obj, name):
    """hasattr() that is safe with Live objects: Live raises RuntimeError (not
    AttributeError) for properties that don't apply, e.g. arrangement_clips on
    the master, return and group tracks."""
    try:
        getattr(obj, name)
        return True
    except Exception:
        return False


def get(obj, name, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


class Unsupported(Exception):
    """Raised when this Live version can't perform an action remotely."""


class MissingItem(Exception):
    """Raised when a device/plugin/sample isn't available on this computer."""


# ---------------------------------------------------------------------------
# Song
# ---------------------------------------------------------------------------

SONG_PROPS = [
    ("tempo", r4),
    ("signature_numerator", int),
    ("signature_denominator", int),
    ("root_note", int),
    ("scale_name", str),
    ("scale_mode", bool),
    ("swing_amount", r4),
    ("groove_amount", r4),
    ("loop", bool),
    ("loop_start", r4),
    ("loop_length", r4),
    ("clip_trigger_quantization", int),
    ("midi_recording_quantization", int),
]


def read_song(song, settings):
    out = {}
    for name, conv in SONG_PROPS:
        if has(song, name):
            try:
                out[name] = conv(getattr(song, name))
            except Exception:
                pass
    if settings.get("sync_transport"):
        out["is_playing"] = bool(song.is_playing)
    return out


def write_song(song, props):
    # loop_start/loop_length must be applied in an order Live accepts.
    for name, conv in SONG_PROPS:
        if name in props and has(song, name) and name not in ("loop_start", "loop_length"):
            _set_if_diff(song, name, conv(props[name]))
    if "loop_start" in props or "loop_length" in props:
        start = props.get("loop_start", song.loop_start)
        length = props.get("loop_length", song.loop_length)
        try:
            song.loop_start = start
            song.loop_length = length
        except Exception:
            song.loop_length = length
            song.loop_start = start
    if "is_playing" in props:
        if props["is_playing"] and not song.is_playing:
            song.start_playing()
        elif not props["is_playing"] and song.is_playing:
            song.stop_playing()


def _set_if_diff(obj, name, value):
    try:
        cur = getattr(obj, name)
        if isinstance(value, float):
            if abs(float(cur) - value) < 1e-5:
                return
        elif cur == value:
            return
    except Exception:
        pass
    setattr(obj, name, value)


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------

def track_kind(track):
    if get(track, "is_foldable", False):
        return "group"
    if get(track, "has_midi_input", False):
        return "midi"
    return "audio"


def read_track(track, settings, is_master=False):
    out = {"kind": "master" if is_master else track_kind(track)}
    if not is_master:
        out["name"] = track.name
        out["color"] = int(get(track, "color", 0))
        for prop in ("mute", "solo"):
            if prop == "solo" and not settings.get("sync_solo", True):
                continue
            if has(track, prop):
                out[prop] = bool(getattr(track, prop))
        if settings.get("sync_arm") and get(track, "can_be_armed", False):
            out["arm"] = bool(track.arm)
        if has(track, "current_monitoring_state") and out["kind"] != "group":
            try:
                out["monitor"] = int(track.current_monitoring_state)
            except Exception:
                pass
        if get(track, "is_grouped", False) and has(track, "fold_state"):
            pass
        if has(track, "fold_state") and out["kind"] == "group":
            out["fold"] = int(track.fold_state)
    mixer = track.mixer_device
    out["volume"] = r4(mixer.volume.value)
    out["pan"] = r4(mixer.panning.value)
    if not is_master:
        out["sends"] = [r4(s.value) for s in mixer.sends]
    if has(mixer, "crossfade_assign") and not is_master:
        try:
            out["xfade"] = int(mixer.crossfade_assign)
        except Exception:
            pass
    return out


def write_track(track, props):
    mixer = track.mixer_device
    for prop in ("name", "color", "mute", "solo", "arm"):
        if prop in props and has(track, prop):
            try:
                _set_if_diff(track, prop, props[prop])
            except Exception:
                pass  # e.g. arm on a track that can't be armed
    if "monitor" in props and has(track, "current_monitoring_state"):
        try:
            _set_if_diff(track, "current_monitoring_state", int(props["monitor"]))
        except Exception:
            pass
    if "fold" in props and has(track, "fold_state"):
        try:
            _set_if_diff(track, "fold_state", int(props["fold"]))
        except Exception:
            pass
    if "volume" in props:
        _set_param(mixer.volume, props["volume"])
    if "pan" in props:
        _set_param(mixer.panning, props["pan"])
    if "sends" in props:
        sends = list(mixer.sends)
        values = props["sends"]
        if isinstance(values, dict):  # sparse patch {index: value}
            items = [(int(k), v) for k, v in values.items()]
        else:
            items = list(enumerate(values))
        for i, v in items:
            if i < len(sends):
                _set_param(sends[i], v)
    if "xfade" in props and has(mixer, "crossfade_assign"):
        try:
            _set_if_diff(mixer, "crossfade_assign", int(props["xfade"]))
        except Exception:
            pass


def _set_param(param, value):
    try:
        v = float(value)
        v = max(param.min, min(param.max, v))
        if abs(param.value - v) > 1e-6:
            param.value = v
    except Exception:
        pass


def read_scene(scene):
    out = {"name": scene.name, "color": int(get(scene, "color", 0))}
    if has(scene, "tempo"):
        out["tempo"] = r4(scene.tempo)
        if has(scene, "tempo_enabled"):
            out["tempo_on"] = bool(scene.tempo_enabled)
    return out


def write_scene(scene, props):
    for prop in ("name", "color", "tempo"):
        if prop in props and has(scene, prop):
            try:
                _set_if_diff(scene, prop, props[prop])
            except Exception:
                pass
    if "tempo_on" in props and has(scene, "tempo_enabled"):
        try:
            _set_if_diff(scene, "tempo_enabled", bool(props["tempo_on"]))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

PLUGIN_CLASSES = ("PluginDevice", "AuPluginDevice")


def syncable_devices(track):
    return [d for d in track.devices if not str(d.name).startswith(IGNORED_DEVICE_PREFIXES)]


def device_signature(device):
    cls = str(device.class_name)
    display = str(get(device, "class_display_name", "") or cls)
    sig = {"cls": cls, "name": str(device.name), "type": int(get(device, "type", 0))}
    if cls in PLUGIN_CLASSES:
        sig["plugin"] = str(device.name)
    else:
        sig["display"] = display
    sig["n"] = len(device.parameters)
    return sig


def same_device(a, b):
    """Two signatures describe the same kind of device (ignoring renames)."""
    if a.get("cls") in PLUGIN_CLASSES and b.get("cls") in PLUGIN_CLASSES:
        return a.get("plugin") == b.get("plugin")  # AU on Mac == VST on Windows
    return a.get("cls") == b.get("cls") and a.get("display") == b.get("display")


def read_device_chain(track):
    return [device_signature(d) for d in syncable_devices(track)]


def read_params(device):
    return [r4(p.value) for p in device.parameters]


def write_params(device, values):
    params = list(device.parameters)
    items = [(int(k), v) for k, v in values.items()] if isinstance(values, dict) else list(enumerate(values))
    for i, v in items:
        if i < len(params):
            p = params[i]
            if get(p, "is_enabled", True):
                _set_param(p, v)


class DeviceLoader(object):
    """Finds devices/plugins in Live's browser and loads them onto tracks."""

    def __init__(self, song, browser_fn):
        self._song = song
        self._browser_fn = browser_fn
        self._index = None

    def _build_index(self):
        browser = self._browser_fn()
        index = {}
        if browser is None:
            self._index = index
            return
        roots = []
        for attr, depth in (("instruments", 2), ("audio_effects", 2), ("midi_effects", 2),
                            ("drums", 2), ("max_for_live", 3), ("plugins", 5)):
            root = getattr(browser, attr, None)
            if root is not None:
                roots.append((root, depth, attr))
        for root, depth, attr in roots:
            stack = [(c, 1) for c in root.children]
            while stack:
                item, d = stack.pop()
                name = str(item.name)
                base = os.path.splitext(name)[0]
                if item.is_loadable:
                    index.setdefault((attr, name.lower()), item)
                    index.setdefault((attr, base.lower()), item)
                if d < depth and get(item, "is_folder", True):
                    try:
                        stack.extend((c, d + 1) for c in item.children)
                    except Exception:
                        pass
        self._index = index

    def find(self, sig):
        if self._index is None:
            self._build_index()
        if sig.get("cls") in PLUGIN_CLASSES:
            return self._index.get(("plugins", sig["plugin"].lower()))
        name = (sig.get("display") or sig.get("name") or "").lower()
        if sig.get("cls", "").startswith("Mx"):
            return self._index.get(("max_for_live", sig.get("name", "").lower()))
        for attr in ("audio_effects", "instruments", "midi_effects", "drums"):
            item = self._index.get((attr, name))
            if item is not None:
                return item
        return None

    def load(self, track, sig):
        # Live 12.1+ can insert native devices directly by name.
        if has(track, "insert_device") and sig.get("cls") not in PLUGIN_CLASSES and not sig.get("cls", "").startswith("Mx"):
            try:
                track.insert_device(sig.get("display") or sig.get("name"), len(list(track.devices)))
                return
            except Exception:
                pass
        item = self.find(sig)
        if item is None:
            raise MissingItem(sig.get("plugin") or sig.get("display") or sig.get("name"))
        browser = self._browser_fn()
        view = self._song.view
        view.selected_track = track
        devices = list(track.devices)
        if devices:
            try:
                view.select_device(devices[-1])
            except Exception:
                pass
        browser.load_item(item)


def write_device_chain(track, target, loader):
    """Make the track's device chain match `target`. Returns list of missing item names."""
    missing = []
    current = syncable_devices(track)
    cur_sigs = [device_signature(d) for d in current]
    keep = 0
    while keep < len(cur_sigs) and keep < len(target) and same_device(cur_sigs[keep], target[keep]):
        keep += 1
    all_devices = list(track.devices)
    for dev in reversed(current[keep:]):
        track.delete_device(all_devices.index(dev))
        all_devices = list(track.devices)
    for sig in target[keep:]:
        try:
            loader.load(track, sig)
        except MissingItem as e:
            missing.append(str(e))
            break  # later devices would end up at the wrong index
    # Restore user-given device names.
    for dev, sig in zip(syncable_devices(track), target):
        if same_device(device_signature(dev), sig) and str(dev.name) != sig.get("name") and sig.get("cls") not in PLUGIN_CLASSES:
            try:
                dev.name = sig["name"]
            except Exception:
                pass
    return missing


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------

CLIP_PROPS = [
    ("name", str), ("color", int), ("looping", bool), ("muted", bool),
    ("signature_numerator", int), ("signature_denominator", int),
    ("launch_mode", int), ("launch_quantization", int), ("legato", bool),
    ("velocity_amount", r4),
]
CLIP_MARKERS = ("loop_start", "loop_end", "start_marker", "end_marker")
AUDIO_PROPS = [("gain", r4), ("pitch_coarse", int), ("pitch_fine", r4), ("warping", bool), ("warp_mode", int), ("ram_mode", bool)]


def read_notes(clip):
    if has(clip, "get_all_notes_extended"):
        notes = clip.get_all_notes_extended()
    else:
        notes = clip.get_notes_extended(0, 128, -NOTE_SPAN, 2 * NOTE_SPAN)
    out = []
    for n in notes:
        out.append([
            int(n.pitch), r6(n.start_time), r6(n.duration), r4(n.velocity), 1 if n.mute else 0,
            r4(get(n, "probability", 1.0)), r4(get(n, "velocity_deviation", 0.0)),
            r4(get(n, "release_velocity", 64.0)),
        ])
    out.sort()
    return out


def write_notes(clip, notes, live_api):
    if read_notes(clip) == notes:
        return
    existing = clip.get_all_notes_extended() if has(clip, "get_all_notes_extended") else clip.get_notes_extended(0, 128, -NOTE_SPAN, 2 * NOTE_SPAN)
    ids = [n.note_id for n in existing if has(n, "note_id")]
    if not existing:
        pass
    elif ids and has(clip, "remove_notes_by_id"):
        clip.remove_notes_by_id(ids)
    else:
        clip.remove_notes_extended(0, 128, -NOTE_SPAN, 2 * NOTE_SPAN)
    if notes:
        specs = tuple(
            live_api.note_spec(pitch=n[0], start_time=n[1], duration=n[2], velocity=n[3], mute=bool(n[4]),
                               probability=n[5], velocity_deviation=n[6], release_velocity=n[7])
            for n in notes
        )
        clip.add_new_notes(specs)


def read_clip(clip, files):
    """files: FileStore used to fingerprint audio. Returns None while a hash is pending."""
    out = {"midi": bool(clip.is_midi_clip)}
    for name, conv in CLIP_PROPS:
        if has(clip, name):
            try:
                out[name] = conv(getattr(clip, name))
            except Exception:
                pass
    for name in CLIP_MARKERS:
        out[name] = r6(getattr(clip, name))
    out["length"] = r6(clip.length)
    if clip.is_midi_clip:
        out["notes"] = read_notes(clip)
    else:
        for name, conv in AUDIO_PROPS:
            if has(clip, name):
                try:
                    out[name] = conv(getattr(clip, name))
                except Exception:
                    pass
        info = files.fingerprint(clip.file_path)
        if info is None:
            return None  # still hashing; try again shortly
        out["file"] = info
        if has(clip, "warp_markers"):
            try:
                out["warp"] = [[r6(m.beat_time), r6(m.sample_time)] for m in clip.warp_markers]
            except Exception:
                pass
    return out


def write_clip_props(clip, props, live_api):
    for name, conv in CLIP_PROPS + AUDIO_PROPS:
        if name in props and has(clip, name):
            try:
                _set_if_diff(clip, name, conv(props[name]))
            except Exception:
                pass
    _write_markers(clip, props)
    if "notes" in props and clip.is_midi_clip:
        write_notes(clip, props["notes"], live_api)
    if "warp" in props and has(clip, "warp_markers") and has(clip, "add_warp_marker"):
        try:
            cur = [[r6(m.beat_time), r6(m.sample_time)] for m in clip.warp_markers]
            if cur != props["warp"]:
                for m in list(clip.warp_markers):
                    try:
                        clip.remove_warp_marker(m.beat_time)
                    except Exception:
                        pass
                for beat, sample in props["warp"]:
                    clip.add_warp_marker(live_api.warp_marker(beat, sample))
        except Exception:
            pass


def _write_markers(clip, props):
    """Loop/start/end markers must stay ordered (start < end) at every step,
    so when moving a region to the right we move its end first."""
    for lo, hi in (("loop_start", "loop_end"), ("start_marker", "end_marker")):
        if lo not in props and hi not in props:
            continue
        new_lo = float(props.get(lo, getattr(clip, lo)))
        order = (hi, lo) if new_lo >= float(getattr(clip, hi)) else (lo, hi)
        for name in order:
            if name in props:
                try:
                    _set_if_diff(clip, name, float(props[name]))
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Clip envelopes (session clip automation)
# ---------------------------------------------------------------------------

ENVELOPE_STEP = 0.25  # one 16th note


def automatable_params(track):
    """(key, param) pairs that can carry clip automation."""
    mixer = track.mixer_device
    out = [("vol", mixer.volume), ("pan", mixer.panning)]
    for i, s in enumerate(mixer.sends):
        out.append(("send%d" % i, s))
    for di, dev in enumerate(syncable_devices(track)):
        for pi, p in enumerate(dev.parameters):
            out.append(("d%dp%d" % (di, pi), p))
    return out


def read_envelopes(clip, track):
    if not has(clip, "automation_envelope"):
        return {}
    start, end = float(clip.loop_start), float(clip.loop_end)
    steps = int(min(4096, max(1, (end - start) / ENVELOPE_STEP)))
    out = {}
    for key, param in automatable_params(track):
        try:
            env = clip.automation_envelope(param)
        except Exception:
            env = None
        if env is None:
            continue
        values = [r4(env.value_at_time(start + i * ENVELOPE_STEP)) for i in range(steps)]
        out[key] = [r6(start)] + _rle(values)
    return out


def _rle(values):
    out = []
    for v in values:
        if out and out[-1][0] == v:
            out[-1][1] += 1
        else:
            out.append([v, 1])
    return out


def write_envelopes(clip, track, envs):
    if not has(clip, "automation_envelope"):
        raise Unsupported("clip automation")
    params = dict(automatable_params(track))
    current = read_envelopes(clip, track)
    for key in set(current) - set(envs):
        if key in params:
            try:
                clip.clear_envelope(params[key])
            except Exception:
                pass
    for key, data in envs.items():
        if key not in params or current.get(key) == data:
            continue
        param = params[key]
        try:
            clip.clear_envelope(param)
        except Exception:
            pass
        env = clip.create_automation_envelope(param)
        t = data[0]
        for value, count in data[1:]:
            env.insert_step(t, count * ENVELOPE_STEP, value)
            t += count * ENVELOPE_STEP


# ---------------------------------------------------------------------------
# Arrangement
# ---------------------------------------------------------------------------

def read_arrangement(track, files):
    clips = get(track, "arrangement_clips", None)
    if clips is None:
        return []  # master/return/group tracks have no arrangement clips
    out = []
    for clip in clips:
        if get(clip, "is_recording", False):
            return None  # wait until the take is finished
        st = read_clip(clip, files)
        if st is None:
            return None
        st["start"] = r6(clip.start_time)
        st["end"] = r6(clip.end_time)
        out.append(st)
    out.sort(key=lambda c: (c["start"], c["end"]))
    return out


def _arr_identity(st):
    ident = (st["start"], st["end"], st["midi"])
    if not st["midi"]:
        ident += (st.get("file", {}).get("hash"),)
    return ident


def write_arrangement(track, target, files, live_api):
    """Reconcile arrangement clips. Clips can't be moved through the API, so a
    moved clip is deleted and re-created at its new position."""
    if not has(track, "arrangement_clips"):
        raise Unsupported("arrangement clips")
    current = list(track.arrangement_clips)
    cur_states = []
    for c in current:
        cur_states.append((c, {"start": r6(c.start_time), "end": r6(c.end_time), "midi": bool(c.is_midi_clip),
                               "file": files.fingerprint(c.file_path) if not c.is_midi_clip else None}))
    wanted = list(target)
    matched = []
    for c, st in cur_states:
        ident = _arr_identity(dict(st, file=st["file"] or {}))
        hit = None
        for w in wanted:
            if _arr_identity(w) == ident:
                hit = w
                break
        if hit is not None:
            wanted.remove(hit)
            matched.append((c, hit))
        else:
            if not has(track, "delete_clip"):
                raise Unsupported("deleting arrangement clips")
            track.delete_clip(c)
    for w in wanted:
        length = w["end"] - w["start"]
        if w["midi"]:
            if not has(track, "create_midi_clip"):
                raise Unsupported("creating arrangement MIDI clips (needs Live 12.2+)")
            clip = track.create_midi_clip(w["start"], length)
        else:
            if not has(track, "create_audio_clip"):
                raise Unsupported("creating arrangement audio clips (needs Live 12.2+)")
            path = files.path_for(w["file"]["hash"])
            if path is None:
                raise MissingItem("audio file %s" % w["file"].get("name"))
            clip = track.create_audio_clip(path, w["start"])
        if clip is None:
            clip = _find_arr_clip(track, w["start"])
        if clip is not None:
            matched.append((clip, w))
    for clip, w in matched:
        write_clip_props(clip, w, live_api)


def _find_arr_clip(track, start):
    for c in track.arrangement_clips:
        if abs(c.start_time - start) < 1e-4:
            return c
    return None


def create_session_clip(slot, state, files):
    if slot.has_clip:
        slot.delete_clip()
    if state["midi"]:
        slot.create_clip(max(0.25, float(state.get("length") or state.get("loop_end", 4.0))))
    else:
        if not has(slot, "create_audio_clip"):
            raise Unsupported("creating audio clips (needs Live 12)")
        path = files.path_for(state["file"]["hash"])
        if path is None:
            raise MissingItem("audio file %s" % state["file"].get("name"))
        slot.create_audio_clip(path)
    return slot.clip
