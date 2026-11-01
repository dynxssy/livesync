# -*- coding: utf-8 -*-
"""Persistent settings in ~/Documents/LiveSync/settings.json."""
from __future__ import absolute_import, print_function, unicode_literals

import json
import os

from .files import default_root

DEFAULTS = {
    "server": "",            # e.g. wss://livesync-relay.fly.dev
    "name": "",
    "room": "",
    "password": "",
    "panel_port": 47010,
    # Session-wide options (shared with everyone in the session):
    "sync_solo": True,
    "sync_arm": False,
    "sync_transport": False,
    "auto_resync": False,
}

SESSION_OPTIONS = ("sync_solo", "sync_arm", "sync_transport", "auto_resync")


class Settings(dict):
    def __init__(self, path=None):
        dict.__init__(self, DEFAULTS)
        self.path = path or os.path.join(default_root(), "settings.json")
        try:
            with open(self.path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.update(data)
        except Exception:
            pass
        if not self.get("name"):
            self["name"] = _default_name()

    def save(self):
        try:
            d = os.path.dirname(self.path)
            if not os.path.isdir(d):
                os.makedirs(d)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dict(self), f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception:
            pass


def _default_name():
    for var in ("USER", "USERNAME"):
        v = os.environ.get(var)
        if v:
            return v[:1].upper() + v[1:]
    return "Producer"
