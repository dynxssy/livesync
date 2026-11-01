#!/usr/bin/env python3
"""Builds the LiveSync Max for Live device.

Outputs:
  LiveSync.maxpat  - the device patcher (open in Max if you want to edit it)
  LiveSync.amxd    - the Max for Live Audio Effect to drop on your Master track

The device is a pass-through audio effect containing a [jweb] browser view that
shows the LiveSync panel served by the Remote Script on 127.0.0.1:47010.
"""
import json
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "http://127.0.0.1:47010/device"
W, H = 540, 169


def box(id_, maxclass, rect, text=None, **kw):
    b = {"id": id_, "maxclass": maxclass, "patching_rect": rect, "numinlets": kw.pop("numinlets", 1),
         "numoutlets": kw.pop("numoutlets", 0)}
    if text is not None:
        b["text"] = text
    b.update(kw)
    return {"box": b}


def line(src, so, dst, di):
    return {"patchline": {"source": [src, so], "destination": [dst, di]}}


def patcher():
    boxes = [
        box("obj-in", "newobj", [20, 20, 60, 22], "plugin~", numinlets=2, numoutlets=2, outlettype=["signal", "signal"]),
        box("obj-out", "newobj", [20, 260, 60, 22], "plugout~", numinlets=2, numoutlets=2, outlettype=["signal", "signal"]),
        box("obj-lb", "newobj", [120, 20, 60, 22], "loadbang", numoutlets=1, outlettype=["bang"]),
        box("obj-dl", "newobj", [120, 50, 70, 22], "delay 800", numinlets=2, numoutlets=1, outlettype=["bang"]),
        box("obj-url", "message", [120, 80, 230, 22], "url " + URL, numinlets=2, numoutlets=1, outlettype=[""]),
        box("obj-web", "jweb", [120, 120, W, H], numinlets=1, numoutlets=1, outlettype=[""],
            presentation=1, presentation_rect=[0, 0, W, H], rendermode=1, url=URL),
        box("obj-reload", "live.text", [400, 20, 20, 20], numinlets=1, numoutlets=2, outlettype=["", ""],
            parameter_enable=1, presentation=1, presentation_rect=[W + 2, 2, 18, 18], text="↻", texton="↻",
            mode=0, varname="reload",
            saved_attribute_attributes={"valueof": {"parameter_longname": "Reload Panel", "parameter_shortname": "Reload",
                                                     "parameter_type": 2, "parameter_mmax": 1,
                                                     "parameter_enum": ["off", "on"], "parameter_invisible": 1}}),
        box("obj-sel", "newobj", [400, 50, 40, 22], "sel 1", numinlets=2, numoutlets=2, outlettype=["bang", ""]),
        box("obj-cmt", "comment", [20, 300, 420, 20],
            "LiveSync panel. Requires the LiveSync Remote Script (Preferences > Link, Tempo & MIDI).",
            numinlets=1),
    ]
    lines = [
        line("obj-in", 0, "obj-out", 0), line("obj-in", 1, "obj-out", 1),
        line("obj-lb", 0, "obj-dl", 0), line("obj-dl", 0, "obj-url", 0), line("obj-url", 0, "obj-web", 0),
        line("obj-reload", 0, "obj-sel", 0), line("obj-sel", 0, "obj-url", 0),
    ]
    return {
        "patcher": {
            "fileversion": 1,
            "appversion": {"major": 8, "minor": 6, "revision": 0, "architecture": "x64", "modernui": 1},
            "classnamespace": "box",
            "rect": [80, 80, 760, 420],
            "openinpresentation": 1,
            "default_fontsize": 10.0,
            "default_fontface": 0,
            "default_fontname": "Arial Bold",
            "gridonopen": 1,
            "gridsize": [8.0, 8.0],
            "boxes": boxes,
            "lines": lines,
            "dependency_cache": [],
            "latency": 0,
            "is_mpe": 0,
            "minimum_live_version": "",
            "minimum_max_version": "",
            "platform_compatibility": 0,
            "autosave": 0,
            "devicewidth": float(W + 22),
            "description": "LiveSync collaboration panel",
            "digest": "",
            "tags": "",
        }
    }


def write_amxd(path, patch):
    data = json.dumps(patch, indent=1).encode("utf-8") + b"\n\x00"
    with open(path, "wb") as f:
        f.write(b"ampf" + struct.pack("<I", 4) + b"aaaa")          # audio effect
        f.write(b"meta" + struct.pack("<I", 4) + struct.pack("<I", 7))
        f.write(b"ptch" + struct.pack("<I", len(data)) + data)


if __name__ == "__main__":
    p = patcher()
    with open(os.path.join(HERE, "LiveSync.maxpat"), "w") as f:
        json.dump(p, f, indent=1)
    write_amxd(os.path.join(HERE, "LiveSync.amxd"), p)
    print("wrote LiveSync.maxpat and LiveSync.amxd")
