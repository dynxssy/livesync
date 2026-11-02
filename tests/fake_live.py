# -*- coding: utf-8 -*-
"""A small in-memory imitation of Live's Python API (Live Object Model).

It mimics the parts LiveSync uses closely enough to exercise the whole sync
path (listeners, track/scene/device/clip creation, notes, envelopes, browser
loading) without Ableton Live. It is *not* Live: behaviour of the real API on
edge cases (e.g. quantised parameters) can differ, which is why the README asks
for a manual check in Live as well.
"""
import itertools
import os

_ptrs = itertools.count(1000)


class Observable(object):
    OBSERVED = ()

    def __init__(self):
        object.__setattr__(self, "_listeners", {})
        object.__setattr__(self, "_live_ptr", next(_ptrs))

    def __getattr__(self, name):
        if name == "arrangement_clips" and self.__dict__.get("_no_arrangement"):
            raise RuntimeError("Main, Group and Return Tracks have no arrangement clips")
        if name.startswith("add_") and name.endswith("_listener"):
            prop = name[4:-9]
            return lambda fn: self._listeners.setdefault(prop, []).append(fn)
        if name.startswith("remove_") and name.endswith("_listener"):
            prop = name[7:-9]
            return lambda fn: self._listeners.get(prop, []).remove(fn)
        if name.endswith("_has_listener"):
            prop = name[:-13]
            return lambda fn: fn in self._listeners.get(prop, [])
        raise AttributeError(name)

    def __setattr__(self, name, value):
        old = self.__dict__.get(name, object())
        object.__setattr__(self, name, value)
        if name in self.OBSERVED and old != value:
            self._fire(name)

    def _fire(self, prop):
        for fn in list(self._listeners.get(prop, [])):
            fn()


class Param(Observable):
    OBSERVED = ("value",)

    def __init__(self, name, value=0.0, lo=0.0, hi=1.0):
        Observable.__init__(self)
        self.name = name
        self.min = lo
        self.max = hi
        self.is_enabled = True
        self.value = value

    def __setattr__(self, name, value):
        if name == "value" and "max" in self.__dict__:
            if value < self.min or value > self.max:
                raise ValueError("Invalid value")
        Observable.__setattr__(self, name, value)


class Mixer(Observable):
    def __init__(self, n_sends=0):
        Observable.__init__(self)
        self.volume = Param("Track Volume", 0.85)
        self.panning = Param("Track Panning", 0.0, -1.0, 1.0)
        self.sends = [Param("Send %d" % i, 0.0) for i in range(n_sends)]


DEVICE_PARAMS = {
    "Eq8": ["Device On"] + ["%d Frequency A" % i for i in range(1, 9)] + ["%d Gain A" % i for i in range(1, 9)],
    "Compressor2": ["Device On", "Threshold", "Ratio", "Attack", "Release", "Output Gain"],
    "OriginalSimpler": ["Device On", "Volume", "Transpose", "Filter Freq"],
    "PluginDevice": ["Device On", "Cutoff", "Resonance", "Drive"],
}
DISPLAY = {"Eq8": "EQ Eight", "Compressor2": "Compressor", "OriginalSimpler": "Simpler"}


class Device(Observable):
    OBSERVED = ("name",)

    def __init__(self, class_name, name=None):
        Observable.__init__(self)
        self.class_name = class_name
        self.class_display_name = DISPLAY.get(class_name, class_name)
        self.name = name or self.class_display_name
        self.type = 1 if class_name in ("OriginalSimpler",) else 2
        self.parameters = [Param(n, 1.0 if i == 0 else 0.5) for i, n in enumerate(DEVICE_PARAMS.get(class_name, ["Device On"]))]


class MidiNote(object):
    _ids = itertools.count(1)

    def __init__(self, pitch, start_time, duration, velocity=100.0, mute=False, probability=1.0,
                 velocity_deviation=0.0, release_velocity=64.0):
        self.pitch = pitch
        self.start_time = start_time
        self.duration = duration
        self.velocity = velocity
        self.mute = mute
        self.probability = probability
        self.velocity_deviation = velocity_deviation
        self.release_velocity = release_velocity
        self.note_id = next(MidiNote._ids)


class Envelope(object):
    def __init__(self, default):
        self.default = default
        self.steps = []  # (start, end, value)

    def value_at_time(self, t):
        for s, e, v in reversed(self.steps):
            if s <= t < e:
                return v
        return self.default

    def insert_step(self, t, length, value):
        self.steps.append((t, t + length, value))


class Clip(Observable):
    OBSERVED = ("name", "color", "loop_start", "loop_end", "start_marker", "end_marker", "looping", "gain",
                "pitch_coarse", "warping", "muted")

    def __init__(self, length=4.0, midi=True, file_path=None, start_time=0.0):
        Observable.__init__(self)
        self.is_midi_clip = midi
        self.is_audio_clip = not midi
        self.name = ""
        self.color = 0
        self.looping = True
        self.muted = False
        self.loop_start = 0.0
        self.loop_end = length
        self.start_marker = 0.0
        self.end_marker = length
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.start_time = start_time
        self.is_recording = False
        self._notes = []
        self._envs = {}
        if not midi:
            self.file_path = file_path
            self.gain = 0.4
            self.pitch_coarse = 0
            self.pitch_fine = 0.0
            self.warping = True
            self.warp_mode = 0
        else:
            self.file_path = None

    @property
    def length(self):
        return self.loop_end - self.loop_start

    @property
    def end_time(self):
        return self.start_time + self.length

    def __setattr__(self, name, value):
        pairs = {"loop_start": ("loop_end", 1), "start_marker": ("end_marker", 1),
                 "loop_end": ("loop_start", -1), "end_marker": ("start_marker", -1)}
        if name in pairs and pairs[name][0] in self.__dict__:
            other, sign = pairs[name]
            if (sign > 0 and value >= self.__dict__[other]) or (sign < 0 and value <= self.__dict__[other]):
                raise RuntimeError("marker order")
        Observable.__setattr__(self, name, value)

    # notes (Live 11 extended API)
    def get_all_notes_extended(self):
        return tuple(self._notes)

    def add_new_notes(self, specs):
        for s in specs:
            self._notes.append(MidiNote(s.pitch, s.start_time, s.duration, s.velocity, s.mute, s.probability,
                                        s.velocity_deviation, s.release_velocity))
        self._fire("notes")

    def remove_notes_by_id(self, ids):
        ids = set(ids)
        self._notes = [n for n in self._notes if n.note_id not in ids]
        self._fire("notes")

    def apply_note_modifications(self, notes):
        self._fire("notes")

    # envelopes
    def automation_envelope(self, param):
        return self._envs.get(param._live_ptr)

    def create_automation_envelope(self, param):
        env = Envelope(param.value)
        self._envs[param._live_ptr] = env
        return env

    def clear_envelope(self, param):
        self._envs.pop(param._live_ptr, None)


class ClipSlot(Observable):
    OBSERVED = ("has_clip",)

    def __init__(self, track):
        Observable.__init__(self)
        self.canonical_parent = track
        self.clip = None
        self.has_clip = False

    def create_clip(self, length):
        if not self.canonical_parent.has_midi_input:
            raise RuntimeError("audio track")
        if self.has_clip:
            raise RuntimeError("slot not empty")
        self.clip = Clip(length)
        self.clip.canonical_parent = self
        self.has_clip = True
        return self.clip

    def create_audio_clip(self, path):
        if not os.path.exists(path):
            raise RuntimeError("file missing")
        self.clip = Clip(4.0, midi=False, file_path=path)
        self.clip.canonical_parent = self
        self.has_clip = True
        return self.clip

    def delete_clip(self):
        self.clip = None
        self.has_clip = False


class Track(Observable):
    OBSERVED = ("name", "color", "mute", "solo", "arm", "devices", "arrangement_clips")

    def __init__(self, song, kind="midi", n_scenes=0, n_sends=0, master=False):
        Observable.__init__(self)
        self.song = song
        self.has_midi_input = kind == "midi"
        self.has_audio_input = kind == "audio"
        self.is_foldable = False
        self.can_be_armed = not master
        self.name = "Master" if master else ("%s %d" % ("MIDI" if kind == "midi" else "Audio", len(song.tracks) + 1 if song else 1))
        self.color = 0
        self.mute = False
        self.solo = False
        self.arm = False
        self.mixer_device = Mixer(0 if master else n_sends)
        self._no_arrangement = master
        self.devices = []
        self.clip_slots = [] if master else [ClipSlot(self) for _ in range(n_scenes)]
        if not master:  # real Live raises RuntimeError on master/return/group
            self.arrangement_clips = []

    def delete_device(self, index):
        self.devices = self.devices[:index] + self.devices[index + 1:]

    def _add_device(self, dev):
        self.devices = self.devices + [dev]

    def create_midi_clip(self, start, length):
        c = Clip(length, start_time=start)
        self.arrangement_clips = sorted(self.arrangement_clips + [c], key=lambda c: c.start_time)
        return c

    def create_audio_clip(self, path, position):
        c = Clip(4.0, midi=False, file_path=path, start_time=position)
        self.arrangement_clips = sorted(self.arrangement_clips + [c], key=lambda c: c.start_time)
        return c

    def delete_clip(self, clip):
        self.arrangement_clips = [c for c in self.arrangement_clips if c is not clip]


class Scene(Observable):
    OBSERVED = ("name", "color")

    def __init__(self):
        Observable.__init__(self)
        self.name = ""
        self.color = 0
        self.tempo = -1.0


class View(object):
    def __init__(self):
        self.selected_track = None
        self.detail_clip = None
        self.selected_device = None

    def select_device(self, d):
        self.selected_device = d


class Song(Observable):
    OBSERVED = ("tempo", "signature_numerator", "signature_denominator", "root_note", "scale_name", "tracks",
                "return_tracks", "scenes", "is_playing", "swing_amount", "loop", "loop_start", "loop_length")

    def __init__(self, n_tracks=2, n_scenes=4):
        Observable.__init__(self)
        self.tempo = 120.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.root_note = 0
        self.scale_name = "Major"
        self.scale_mode = False
        self.swing_amount = 0.0
        self.groove_amount = 1.0
        self.loop = False
        self.loop_start = 0.0
        self.loop_length = 16.0
        self.clip_trigger_quantization = 4
        self.midi_recording_quantization = 0
        self.is_playing = False
        self.view = View()
        self.tracks = []
        self.return_tracks = []
        self.scenes = [Scene() for _ in range(n_scenes)]
        self.master_track = Track(self, "audio", master=True)
        for i in range(n_tracks):
            self.create_midi_track(-1) if i % 2 == 0 else self.create_audio_track(-1)

    def _new_track(self, index, kind):
        t = Track(self, kind, len(self.scenes), len(self.return_tracks))
        tracks = list(self.tracks)
        tracks.insert(len(tracks) if index < 0 else index, t)
        self.tracks = tracks
        return t

    def create_midi_track(self, index=-1):
        return self._new_track(index, "midi")

    def create_audio_track(self, index=-1):
        return self._new_track(index, "audio")

    def delete_track(self, index):
        tracks = list(self.tracks)
        del tracks[index]
        self.tracks = tracks

    def create_return_track(self):
        t = Track(self, "audio", 0, 0)
        t._no_arrangement = True
        t.__dict__.pop("arrangement_clips", None)
        t.name = "%s-Return" % "ABCDEFGH"[len(self.return_tracks)]
        self.return_tracks = self.return_tracks + [t]
        for tr in self.tracks + self.return_tracks:
            while len(tr.mixer_device.sends) < len(self.return_tracks):
                tr.mixer_device.sends.append(Param("Send", 0.0))
        return t

    def delete_return_track(self, index):
        rt = list(self.return_tracks)
        del rt[index]
        self.return_tracks = rt
        for tr in self.tracks + self.return_tracks:
            del tr.mixer_device.sends[index]

    def create_scene(self, index=-1):
        s = Scene()
        scenes = list(self.scenes)
        idx = len(scenes) if index < 0 else index
        scenes.insert(idx, s)
        for t in self.tracks:
            t.clip_slots.insert(idx, ClipSlot(t))
        self.scenes = scenes
        return s

    def delete_scene(self, index):
        scenes = list(self.scenes)
        del scenes[index]
        for t in self.tracks:
            del t.clip_slots[index]
        self.scenes = scenes

    def start_playing(self):
        self.is_playing = True

    def stop_playing(self):
        self.is_playing = False


class BrowserItem(object):
    def __init__(self, name, factory=None, children=()):
        self.name = name
        self.factory = factory
        self.children = list(children)
        self.is_loadable = factory is not None
        self.is_folder = bool(children)


class Browser(object):
    def __init__(self, song, plugins=("Serum", "Pro-Q 3")):
        self.song = song
        self.instruments = BrowserItem("Instruments", children=[BrowserItem("Simpler", lambda: Device("OriginalSimpler"))])
        self.audio_effects = BrowserItem("Audio Effects", children=[
            BrowserItem("EQ Eight", lambda: Device("Eq8"), children=[BrowserItem("Preset.adv")]),
            BrowserItem("Compressor", lambda: Device("Compressor2")),
        ])
        self.midi_effects = BrowserItem("MIDI Effects")
        self.drums = BrowserItem("Drums")
        self.max_for_live = BrowserItem("M4L")
        self.plugins = BrowserItem("Plug-ins", children=[
            BrowserItem("VST3", children=[BrowserItem(p, (lambda p=p: Device("PluginDevice", p))) for p in plugins])])

    def load_item(self, item):
        self.song.view.selected_track._add_device(item.factory())


class FakeLiveApi(object):
    class Spec(object):
        def __init__(self, **kw):
            self.__dict__.update(kw)

    @staticmethod
    def note_spec(**kw):
        return FakeLiveApi.Spec(**kw)

    @staticmethod
    def warp_marker(beat, sample):
        return FakeLiveApi.Spec(beat_time=beat, sample_time=sample)
