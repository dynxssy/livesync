{
 "patcher": {
  "fileversion": 1,
  "appversion": {
   "major": 8,
   "minor": 6,
   "revision": 0,
   "architecture": "x64",
   "modernui": 1
  },
  "classnamespace": "box",
  "rect": [
   80,
   80,
   760,
   420
  ],
  "openinpresentation": 1,
  "default_fontsize": 10.0,
  "default_fontface": 0,
  "default_fontname": "Arial Bold",
  "gridonopen": 1,
  "gridsize": [
   8.0,
   8.0
  ],
  "boxes": [
   {
    "box": {
     "id": "obj-in",
     "maxclass": "newobj",
     "patching_rect": [
      20,
      20,
      60,
      22
     ],
     "numinlets": 2,
     "numoutlets": 2,
     "text": "plugin~",
     "outlettype": [
      "signal",
      "signal"
     ]
    }
   },
   {
    "box": {
     "id": "obj-out",
     "maxclass": "newobj",
     "patching_rect": [
      20,
      260,
      60,
      22
     ],
     "numinlets": 2,
     "numoutlets": 2,
     "text": "plugout~",
     "outlettype": [
      "signal",
      "signal"
     ]
    }
   },
   {
    "box": {
     "id": "obj-lb",
     "maxclass": "newobj",
     "patching_rect": [
      120,
      20,
      60,
      22
     ],
     "numinlets": 1,
     "numoutlets": 1,
     "text": "loadbang",
     "outlettype": [
      "bang"
     ]
    }
   },
   {
    "box": {
     "id": "obj-dl",
     "maxclass": "newobj",
     "patching_rect": [
      120,
      50,
      70,
      22
     ],
     "numinlets": 2,
     "numoutlets": 1,
     "text": "delay 800",
     "outlettype": [
      "bang"
     ]
    }
   },
   {
    "box": {
     "id": "obj-url",
     "maxclass": "message",
     "patching_rect": [
      120,
      80,
      230,
      22
     ],
     "numinlets": 2,
     "numoutlets": 1,
     "text": "url http://127.0.0.1:47010/device",
     "outlettype": [
      ""
     ]
    }
   },
   {
    "box": {
     "id": "obj-web",
     "maxclass": "jweb",
     "patching_rect": [
      120,
      120,
      540,
      169
     ],
     "numinlets": 1,
     "numoutlets": 1,
     "outlettype": [
      ""
     ],
     "presentation": 1,
     "presentation_rect": [
      0,
      0,
      540,
      169
     ],
     "rendermode": 1,
     "url": "http://127.0.0.1:47010/device"
    }
   },
   {
    "box": {
     "id": "obj-reload",
     "maxclass": "live.text",
     "patching_rect": [
      400,
      20,
      20,
      20
     ],
     "numinlets": 1,
     "numoutlets": 2,
     "text": "\u21bb",
     "outlettype": [
      "",
      ""
     ],
     "parameter_enable": 1,
     "presentation": 1,
     "presentation_rect": [
      542,
      2,
      18,
      18
     ],
     "texton": "\u21bb",
     "mode": 0,
     "varname": "reload",
     "saved_attribute_attributes": {
      "valueof": {
       "parameter_longname": "Reload Panel",
       "parameter_shortname": "Reload",
       "parameter_type": 2,
       "parameter_mmax": 1,
       "parameter_enum": [
        "off",
        "on"
       ],
       "parameter_invisible": 1
      }
     }
    }
   },
   {
    "box": {
     "id": "obj-sel",
     "maxclass": "newobj",
     "patching_rect": [
      400,
      50,
      40,
      22
     ],
     "numinlets": 2,
     "numoutlets": 2,
     "text": "sel 1",
     "outlettype": [
      "bang",
      ""
     ]
    }
   },
   {
    "box": {
     "id": "obj-cmt",
     "maxclass": "comment",
     "patching_rect": [
      20,
      300,
      420,
      20
     ],
     "numinlets": 1,
     "numoutlets": 0,
     "text": "LiveSync panel. Requires the LiveSync Remote Script (Preferences > Link, Tempo & MIDI)."
    }
   }
  ],
  "lines": [
   {
    "patchline": {
     "source": [
      "obj-in",
      0
     ],
     "destination": [
      "obj-out",
      0
     ]
    }
   },
   {
    "patchline": {
     "source": [
      "obj-in",
      1
     ],
     "destination": [
      "obj-out",
      1
     ]
    }
   },
   {
    "patchline": {
     "source": [
      "obj-lb",
      0
     ],
     "destination": [
      "obj-dl",
      0
     ]
    }
   },
   {
    "patchline": {
     "source": [
      "obj-dl",
      0
     ],
     "destination": [
      "obj-url",
      0
     ]
    }
   },
   {
    "patchline": {
     "source": [
      "obj-url",
      0
     ],
     "destination": [
      "obj-web",
      0
     ]
    }
   },
   {
    "patchline": {
     "source": [
      "obj-reload",
      0
     ],
     "destination": [
      "obj-sel",
      0
     ]
    }
   },
   {
    "patchline": {
     "source": [
      "obj-sel",
      0
     ],
     "destination": [
      "obj-url",
      0
     ]
    }
   }
  ],
  "dependency_cache": [],
  "latency": 0,
  "is_mpe": 0,
  "minimum_live_version": "",
  "minimum_max_version": "",
  "platform_compatibility": 0,
  "autosave": 0,
  "devicewidth": 562.0,
  "description": "LiveSync collaboration panel",
  "digest": "",
  "tags": ""
 }
}