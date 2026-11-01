# -*- coding: utf-8 -*-
"""End-to-end test: two simulated Live sets syncing through a real relay server.

Run:  python3 -m unittest discover -s tests -v     (needs Node.js for the relay)
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "remote_script"))
sys.path.insert(0, HERE)

import fake_live as fl  # noqa: E402
from LiveSync.engine import Engine  # noqa: E402
from LiveSync.files import FileStore  # noqa: E402
from LiveSync.net import NetworkClient  # noqa: E402
from LiveSync.settings import Settings  # noqa: E402


class RecordingUI(object):
    def __init__(self):
        self.alerts = []
        self.toasts = []
        self.current_alert = None
        self.prog = None
        self._progress_label = ""
        self.st = {}

    def state(self, **kw):
        self.st.update(kw)

    def alert(self, text, action=None):
        self.current_alert = (text, action) if text else None
        if text:
            self.alerts.append((text, action))

    def progress(self, frac, label=""):
        self.prog = None if frac is None else (frac, label)
        self._progress_label = label if frac is not None else ""

    def toast(self, text):
        self.toasts.append(text)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Peer(object):
    def __init__(self, name, tmp, url, song=None):
        self.song = song or fl.Song()
        self.browser = fl.Browser(self.song)
        self.ui = RecordingUI()
        root = os.path.join(tmp, name)
        os.makedirs(root)
        self.settings = Settings(path=os.path.join(root, "settings.json"))
        self.settings.update(server=url, name=name)
        self.net = NetworkClient()
        self.engine = Engine(self.song, fl.FakeLiveApi(), self.net, FileStore(root=root), self.ui, self.settings,
                             log=lambda m: sys.stderr.write("[%s] %s\n" % (name, m)) if "error" in m else None,
                             browser_fn=lambda: self.browser)

    def tick(self):
        self.engine.tick()


class SyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="livesync-test-")
        cls.port = free_port()
        env = dict(os.environ, PORT=str(cls.port), DATA_DIR=os.path.join(cls.tmp, "relay"))
        cls.relay = subprocess.Popen(["node", os.path.join(ROOT, "server", "src", "server.js")], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        cls.url = "ws://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.relay.terminate()
        cls.relay.wait()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------
    def setUp(self):
        n = self.id().split(".")[-1]
        self.a = Peer("alice-" + n, self.tmp, self.url)
        self.b = Peer("bob-" + n, self.tmp, self.url, song=fl.Song(n_tracks=3, n_scenes=2))
        self.a.engine.host()
        self.run_until(lambda: self.a.engine.phase == "live" and self.a.engine.room)
        self.b.engine.join(self.a.engine.room)
        self.run_until(lambda: self.b.engine.phase == "live" and self.b.engine.snapshot_job is None)
        self.settle()

    def tearDown(self):
        self.a.engine.shutdown()
        self.b.engine.shutdown()

    def run_until(self, cond, timeout=10.0, peers=None):
        peers = peers or [self.a, self.b]
        deadline = time.time() + timeout
        while time.time() < deadline:
            for p in peers:
                p.tick()
            if cond():
                return
            time.sleep(0.01)
        self.fail("timed out waiting; A=%s B=%s alerts A=%s B=%s" % (
            self.a.engine.phase, self.b.engine.phase, self.a.ui.alerts, self.b.ui.alerts))

    def settle(self, seconds=0.6):
        end = time.time() + seconds
        while time.time() < end:
            self.a.tick()
            self.b.tick()
            time.sleep(0.01)

    def state(self, peer):
        return dict((k, v) for k, v in peer.engine.full_state().items())

    def assertConverged(self, timeout=8.0):
        def same():
            return self.state(self.a) == self.state(self.b)
        try:
            self.run_until(same, timeout=timeout)
        except AssertionError:
            sa, sb = self.state(self.a), self.state(self.b)
            diffs = [(k, sa.get(k), sb.get(k)) for k in sorted(set(sa) | set(sb)) if sa.get(k) != sb.get(k)]
            self.fail("sets differ: %r" % diffs[:5])

    # -- tests -----------------------------------------------------------------
    def test_join_copies_project(self):
        self.assertEqual(len(self.b.song.tracks), len(self.a.song.tracks))
        self.assertEqual([t.has_midi_input for t in self.b.song.tracks], [t.has_midi_input for t in self.a.song.tracks])
        self.assertEqual(len(self.b.song.scenes), len(self.a.song.scenes))
        self.assertConverged()

    def test_song_settings(self):
        self.a.song.tempo = 128.5
        self.a.song.root_note = 9
        self.a.song.scale_name = "Minor"
        self.a.song.signature_numerator = 7
        self.run_until(lambda: self.b.song.tempo == 128.5 and self.b.song.scale_name == "Minor")
        self.assertEqual(self.b.song.root_note, 9)
        self.assertEqual(self.b.song.signature_numerator, 7)
        self.b.song.swing_amount = 0.3
        self.run_until(lambda: self.a.song.swing_amount == 0.3)
        self.assertConverged()

    def test_tracks_mixer_and_scenes(self):
        self.a.song.create_midi_track(0)
        self.a.song.tracks[0].name = "Bass"
        self.a.song.create_return_track()
        self.run_until(lambda: len(self.b.song.tracks) == 3 and self.b.song.tracks[0].name == "Bass")
        self.assertTrue(self.b.song.tracks[0].has_midi_input)
        self.run_until(lambda: len(self.b.song.return_tracks) == 1)
        t = self.b.song.tracks[1]
        t.mixer_device.volume.value = 0.5
        t.mixer_device.panning.value = -0.25
        t.mute = True
        t.mixer_device.sends[0].value = 0.7
        self.run_until(lambda: self.a.song.tracks[1].mixer_device.panning.value == -0.25
                       and self.a.song.tracks[1].mixer_device.sends[0].value == 0.7)
        self.assertTrue(self.a.song.tracks[1].mute)
        self.assertEqual(self.a.song.tracks[1].mixer_device.volume.value, 0.5)
        self.a.song.create_scene(0)
        self.a.song.scenes[0].name = "Intro"
        self.run_until(lambda: len(self.b.song.scenes) == 5 and self.b.song.scenes[0].name == "Intro")
        self.a.song.delete_track(2)
        self.run_until(lambda: len(self.b.song.tracks) == 2)
        self.assertConverged()

    def test_midi_clip_notes(self):
        slot = self.a.song.tracks[0].clip_slots[1]
        clip = slot.create_clip(8.0)
        clip.name = "Hook"
        clip.add_new_notes([fl.FakeLiveApi.note_spec(pitch=60, start_time=0.0, duration=0.5, velocity=100, mute=False,
                                                     probability=1.0, velocity_deviation=0.0, release_velocity=64)])
        clip.add_new_notes([fl.FakeLiveApi.note_spec(pitch=64, start_time=1.0, duration=0.25, velocity=80, mute=False,
                                                     probability=0.5, velocity_deviation=0.0, release_velocity=64)])
        bslot = self.b.song.tracks[0].clip_slots[1]
        self.run_until(lambda: bslot.has_clip and len(bslot.clip.get_all_notes_extended()) == 2)
        self.assertEqual(bslot.clip.name, "Hook")
        self.assertEqual(bslot.clip.loop_end, 8.0)
        # Bob changes a velocity and deletes the other note.
        bclip = bslot.clip
        n = [x for x in bclip._notes if x.pitch == 60][0]
        n.velocity = 33.0
        bclip._notes = [n]
        bclip._fire("notes")
        self.run_until(lambda: [(x.pitch, x.velocity) for x in clip.get_all_notes_extended()] == [(60, 33.0)])
        # Loop moved to the right past the old end (marker ordering).
        clip.loop_end = 16.0
        clip.loop_start = 8.0
        self.run_until(lambda: bclip.loop_start == 8.0 and bclip.loop_end == 16.0)
        slot.delete_clip()
        self.run_until(lambda: not bslot.has_clip)
        self.assertConverged()

    def test_devices_plugins_and_params(self):
        track = self.a.song.tracks[0]
        self.a.song.view.selected_track = track
        self.a.browser.load_item(self.a.browser.audio_effects.children[0])  # EQ Eight
        self.a.browser.load_item(self.a.browser.plugins.children[0].children[0])  # Serum
        btrack = self.b.song.tracks[0]
        self.run_until(lambda: [d.name for d in btrack.devices] == ["EQ Eight", "Serum"])
        track.devices[0].parameters[3].value = 0.9
        track.devices[1].parameters[1].value = 0.1
        self.run_until(lambda: btrack.devices[0].parameters[3].value == 0.9 and btrack.devices[1].parameters[1].value == 0.1)
        # Master chain ("mastering")
        self.b.song.view.selected_track = self.b.song.master_track
        self.b.browser.load_item(self.b.browser.audio_effects.children[1])  # Compressor
        self.run_until(lambda: [d.name for d in self.a.song.master_track.devices] == ["Compressor"])
        self.b.song.master_track.devices[0].parameters[1].value = 0.2
        self.run_until(lambda: self.a.song.master_track.devices[0].parameters[1].value == 0.2)
        # Remove a device
        track.delete_device(0)
        self.run_until(lambda: [d.name for d in btrack.devices] == ["Serum"])
        self.assertConverged()

    def test_missing_plugin_is_reported_softly(self):
        self.b.browser.plugins.children[0].children = [c for c in self.b.browser.plugins.children[0].children if c.name != "Serum"]
        self.b.engine.loader._index = None
        track = self.a.song.tracks[0]
        self.a.song.view.selected_track = track
        self.a.browser.load_item(self.a.browser.plugins.children[0].children[0])  # Serum
        self.run_until(lambda: any("Serum" in t for t, _ in self.b.ui.alerts))
        self.settle(1.0)
        # Alice keeps her plugin: the failure on Bob's side must not delete it.
        self.assertEqual([d.name for d in track.devices], ["Serum"])

    def test_audio_file_is_shared(self):
        path = os.path.join(self.tmp, "kick %s.wav" % self.id().split(".")[-1])
        with open(path, "wb") as f:
            f.write(os.urandom(700000))  # ~3 chunks
        track = self.a.song.tracks[1]  # audio track
        track.clip_slots[0].create_audio_clip(path)
        track.clip_slots[0].clip.gain = 0.6
        bslot = self.b.song.tracks[1].clip_slots[0]
        self.run_until(lambda: bslot.has_clip and bslot.clip.gain == 0.6, timeout=15)
        with open(path, "rb") as f1, open(bslot.clip.file_path, "rb") as f2:
            self.assertEqual(f1.read(), f2.read())
        self.assertTrue(bslot.clip.file_path.endswith("kick test_audio_file_is_shared.wav"))
        self.assertConverged()

    def test_arrangement_clips(self):
        t = self.a.song.tracks[0]
        c = t.create_midi_clip(16.0, 8.0)
        c.add_new_notes([fl.FakeLiveApi.note_spec(pitch=48, start_time=0.0, duration=1.0, velocity=90, mute=False,
                                                  probability=1.0, velocity_deviation=0.0, release_velocity=64)])
        t._fire("arrangement_clips")
        bt = self.b.song.tracks[0]
        self.run_until(lambda: len(bt.arrangement_clips) == 1 and len(bt.arrangement_clips[0]._notes) == 1)
        self.assertEqual(bt.arrangement_clips[0].start_time, 16.0)
        # "Move" the clip: delete and recreate later in the timeline
        t.delete_clip(c)
        c2 = t.create_midi_clip(32.0, 8.0)
        self.run_until(lambda: len(bt.arrangement_clips) == 1 and bt.arrangement_clips[0].start_time == 32.0)
        self.assertConverged()

    def test_clip_automation(self):
        t = self.a.song.tracks[0]
        clip = t.clip_slots[0].create_clip(4.0)
        self.run_until(lambda: self.b.song.tracks[0].clip_slots[0].has_clip)
        env = clip.create_automation_envelope(t.mixer_device.volume)
        env.insert_step(0.0, 2.0, 0.2)
        env.insert_step(2.0, 2.0, 0.9)
        self.a.song.view.detail_clip = clip
        bclip = self.b.song.tracks[0].clip_slots[0].clip
        btrack = self.b.song.tracks[0]
        self.run_until(lambda: bclip.automation_envelope(btrack.mixer_device.volume) is not None
                       and bclip.automation_envelope(btrack.mixer_device.volume).value_at_time(3.0) == 0.9, timeout=8)
        self.assertEqual(bclip.automation_envelope(btrack.mixer_device.volume).value_at_time(1.0), 0.2)

    def test_simultaneous_edits_converge(self):
        pa = self.a.song.tracks[0].mixer_device.volume
        pb = self.b.song.tracks[0].mixer_device.volume
        for i in range(10):
            pa.value = 0.1 + i * 0.01
            pb.value = 0.6 + i * 0.01
            self.a.tick()
            self.b.tick()
        self.settle(1.0)
        self.assertEqual(pa.value, pb.value)
        self.assertConverged()

    def test_drift_is_detected_and_fixed(self):
        # Make Bob miss a change (as if a message were lost) by editing his set
        # while pretending it came from Alice.
        bt = self.b.song.tracks[0]
        bt.name = "Drifted"
        self.b.engine.shadow["t:" + self.b.engine.uid_by_ptr[bt._live_ptr]] = self.b.engine._read(
            "t:" + self.b.engine.uid_by_ptr[bt._live_ptr])
        self.run_until(lambda: self.b.engine.out_of_sync or self.a.engine.out_of_sync, timeout=15)
        alerting = self.b if self.b.engine.out_of_sync else self.a
        self.assertEqual(alerting.ui.current_alert[1], "resync")
        self.b.engine.resync()
        self.run_until(lambda: self.b.song.tracks[0].name != "Drifted" and self.b.engine.phase == "live"
                       and self.b.engine.snapshot_job is None)
        self.assertConverged()

    def test_reconnect_catches_up(self):
        self.b.engine.net.stop()
        self.a.song.tempo = 90.0
        self.a.song.tracks[0].name = "While you were away"
        self.settle(0.3)
        self.b.engine.net.start(self.url)
        self.run_until(lambda: self.b.song.tempo == 90.0 and self.b.song.tracks[0].name == "While you were away")
        self.assertConverged()

    def test_session_options_are_shared(self):
        self.a.engine.set_option("sync_arm", True)
        self.run_until(lambda: self.b.settings.get("sync_arm") is True)
        self.a.song.tracks[0].arm = True
        self.run_until(lambda: self.b.song.tracks[0].arm is True)
        self.assertConverged()


if __name__ == "__main__":
    unittest.main()
