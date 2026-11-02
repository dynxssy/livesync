# What LiveSync syncs

LiveSync can only see and change what Ableton exposes through the Live Object Model (the
Python API that Remote Scripts use). This page lists exactly what that covers.

## Synced

| Area | What |
|---|---|
| Song | Tempo (BPM), time signature, root note and scale (key), scale mode, swing amount, global groove amount, loop on/off and loop region, clip launch quantization, MIDI record quantization |
| Transport | Play and stop (optional, **Play / Stop** toggle) |
| Tracks | Create and delete MIDI, audio and return tracks; name, color, mute, solo (optional), arm (optional), monitoring, fold state of groups |
| Mixer | Volume, pan, every send, crossfader assignment, on every track, return and the master |
| Scenes | Create, delete, name, color, scene tempo |
| Devices | Add and remove Live's instruments, audio effects and MIDI effects; VST, VST3 and AU plugins (matched by name, so an AU on a Mac pairs with the VST3 on Windows); Max for Live devices; device names. Works on every track including the master chain, so mastering chains sync too. |
| Device parameters | Every parameter Live exposes: all knobs on Live devices, Rack macros, and plugin parameters Live lists (use the plugin's **Configure** button to expose more) |
| Session clips | Create and delete MIDI and audio clips; name, color, loop on/off, loop start and end, start and end markers, clip time signature, launch mode, launch quantization, legato, velocity amount, clip mute |
| MIDI notes | Add, delete, move, lengthen and shorten, transpose, velocity, note mute, probability, velocity deviation (velocity range), release velocity |
| Drum Racks, Racks and Simpler | The sounds inside them: the sample in each Simpler (sent to your partner like any audio file), which Drum Rack pads have sounds, the devices on each pad or chain, and their knobs. If your partner has the same kit preset in their browser (for example a Live pack kit), it loads directly. Otherwise LiveSync rebuilds the rack pad by pad. Loading a sample into Simpler needs Live 12, and adding sounds to empty pads needs a Live version whose API can create rack chains. If yours can't, the panel says so. |
| Audio clips | The audio file itself (recorded takes, imported samples, rendered or bounced audio you drop in), gain, transpose, detune, warp on/off, warp mode, RAM mode, warp markers |
| Clip automation | Envelopes inside session clips (volume, pan, sends and any device parameter), synced for the clip you have open in Detail View. Resync copies all clip envelopes. |
| Arrangement | MIDI and audio clips on each track with their notes, audio and clip settings; deleting and moving clips (a moved clip is re-created at its new position). Creating arrangement clips needs a Live 12 version whose API has `Track.create_midi_clip` and `Track.create_audio_clip`. On older versions LiveSync tells you instead of failing silently. |
| Recording | MIDI and audio you record show up for your partner once each take finishes. Audio is uploaded in the background. |

## Not synced, because Live's API doesn't allow it

| Thing | Why, and the workaround |
|---|---|
| Arrangement automation lanes | The API can't read or write automation breakpoints in the Arrangement. Draw the automation in clip envelopes instead, or use session clips. |
| MPE / note expression ("slides" as per-note pitch bends) | Per-note expression curves aren't exposed. Glide and portamento *parameters* on synths do sync. |
| Plugin presets and hidden state | Only parameters Live lists are visible. Wavetables, samples or presets loaded inside a plugin aren't. Load the same preset on both sides, or expose more parameters with **Configure**. |
| Sampler (multisample) zones, plugins inside Racks | The API can't load multisample zones or plugins into a rack chain. Save the rack as a preset and share the file. |
| Group tracks | The API can't create groups. A partner's group shows up as an audio track with the same name. |
| Reordering tracks | The API has no "move track". Moving tracks isn't synced; the panel tells you if it happens. |
| Export / Render, Freeze, Flatten, Consolidate, Slice to new MIDI track | These are UI-only commands. Their *results* sync where possible: drop a rendered file into a clip and it transfers, and a consolidated clip syncs as a new clip. |
| Take lanes / comping (Live 12) | Not exposed by the API. |
| Undo history | Each person's Undo works on their own computer, and the undone result then syncs like any other edit. |
| Selection, view, zoom, browser, preferences, audio and MIDI I/O settings | These are personal to each computer, by design. |
| Hearing each other play in real time | LiveSync syncs the project, not the audio stream. Each computer plays its own copy. |

## How conflicts are handled

* Every change gets a number from the relay, and both computers apply changes in that order.
  If you both move the same control at the same moment, the change the relay receives last
  wins on both computers.
* Every few seconds, when nobody is editing, both computers compare a fingerprint of the set.
  If they differ twice in a row, the panel shows **Fix it**, which copies the host's set.
