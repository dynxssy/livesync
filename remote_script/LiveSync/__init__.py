# -*- coding: utf-8 -*-
"""LiveSync: real-time collaboration for Ableton Live.

Select "LiveSync" as a Control Surface in Live's Preferences > Link, Tempo & MIDI
(no MIDI input/output needed).
"""
from __future__ import absolute_import, print_function, unicode_literals


def create_instance(c_instance):
    from .surface import LiveSyncSurface
    return LiveSyncSurface(c_instance)
