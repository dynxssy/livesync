# -*- coding: utf-8 -*-
"""Glue between Live's Remote Script host and the sync engine."""
from __future__ import absolute_import, print_function, unicode_literals

import io
import os
import time
import traceback

import Live  # provided by Ableton Live

from .engine import Engine
from .files import FileStore, default_root
from .net import NetworkClient
from .settings import Settings
from .ui import Panel

VERSION = "0.1.0"


class LiveApi(object):
    """The few constructors the engine needs from Live's C++ API."""

    @staticmethod
    def note_spec(**kw):
        return Live.Clip.MidiNoteSpecification(**kw)

    @staticmethod
    def warp_marker(beat_time, sample_time):
        return Live.Clip.WarpMarker(sample_time=sample_time, beat_time=beat_time)


class FileLog(object):
    def __init__(self, c_instance):
        self._c = c_instance
        self._path = os.path.join(default_root(), "livesync.log")
        try:
            if os.path.exists(self._path) and os.path.getsize(self._path) > 2 * 1024 * 1024:
                os.replace(self._path, self._path + ".1")
        except Exception:
            pass

    def __call__(self, msg):
        line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            with io.open(self._path, "a", encoding="utf-8") as f:
                f.write(line if isinstance(line, type(u"")) else line.decode("utf-8", "replace"))
        except Exception:
            pass
        try:
            self._c.log_message("LiveSync: " + str(msg)[:500])
        except Exception:
            pass


class LiveSyncSurface(object):
    """Minimal Remote Script (no MIDI controller involved)."""

    def __init__(self, c_instance):
        self._c = c_instance
        self.log = FileLog(c_instance)
        self.engine = None
        self.panel = None
        try:
            self.settings = Settings()
            self.panel = Panel(self.settings, show_message=self._show, port=int(self.settings.get("panel_port") or 47010), log=self.log)
            if not self.panel.start():
                self._show("LiveSync: couldn't open the panel port. Is another copy of Live running?")
            self.engine = Engine(
                song=c_instance.song(),
                live_api=LiveApi(),
                net=NetworkClient(log=self.log),
                files=FileStore(log=self.log),
                ui=self.panel,
                settings=self.settings,
                log=self.log,
                browser_fn=lambda: Live.Application.get_application().browser,
                show_message=self._show,
            )
            self.log("LiveSync %s loaded (Live %s)" % (VERSION, self._live_version()))
            self._show("LiveSync ready. Open the LiveSync device or http://127.0.0.1:%d" % self.panel.port)
        except Exception:
            self.log("startup failed:\n" + traceback.format_exc())
            self._show("LiveSync couldn't start. Details are in Documents/LiveSync/livesync.log")

    def _live_version(self):
        try:
            app = Live.Application.get_application()
            return "%d.%d.%d" % (app.get_major_version(), app.get_minor_version(), app.get_bugfix_version())
        except Exception:
            return "?"

    def _show(self, msg):
        try:
            self._c.show_message(msg)
        except Exception:
            pass

    # -- Remote Script interface -------------------------------------------------
    def update_display(self):
        """Called by Live roughly every 100 ms on the main thread."""
        if self.engine is None:
            return
        try:
            for cmd in self.panel.poll_commands():
                self.engine.handle_command(cmd)
            self.engine.tick()
        except Exception:
            self.log("tick failed:\n" + traceback.format_exc())

    def disconnect(self):
        try:
            if self.engine is not None:
                self.engine.shutdown()
            if self.panel is not None:
                self.panel.stop()
        except Exception:
            pass

    def connect_script_instances(self, instanciated_scripts):
        pass

    def refresh_state(self):
        pass

    def build_midi_map(self, midi_map_handle):
        pass

    def receive_midi(self, midi_bytes):
        pass

    def can_lock_to_devices(self):
        return False

    def suggest_input_port(self):
        return ""

    def suggest_output_port(self):
        return ""

    def suggest_map_mode(self, cc_no, channel):
        return -1

    def request_rebuild_midi_map(self):
        pass

    def application(self):
        return Live.Application.get_application()

    def song(self):
        return self._c.song()
