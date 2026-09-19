# Deckplate

A small driver and controller for the SOOMFON Stream Controller, a 15 key LCD
macro deck that is really a Mirabox Stream Dock 293S underneath. Written to
replace the official Windows only software with something that runs on Linux
Mint and Windows from one code base, and does only the basics: pictures on
keys, a clock, the weather, hotkeys and launching programs.

Current state: the driver, the controller and the configuration page all
work on real hardware. A TOML config file describes pages of keys with
pictures, labels and actions, the display strip shows a clock, the date and
the weather, and a configuration page served by the daemon edits all of it
live. See `REVIEW.md` for the full research and hardware findings and
`docs/gui-review.md` for why the page is built the way it is.

## What the deck looks like to the software

```
13  10   7   4   1  |  16
14  11   8   5   2  |  17
15  12   9   6   3  |  18
```

The firmware numbers keys down each column starting from the right. The right
hand column is a display only strip, three screens with no button under them.
The three physical side buttons beside that strip send nothing at all. The
`deckplate.layout` module hides all of this: everything else speaks in rows
and columns from the top left.

## Running from source

Python 3.11 or newer.

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m deckplate devices
.venv/bin/python -m deckplate probe
```

`--system-site-packages` is needed on Linux and only there. The configuration
page opens in a real window through PyGObject, which apt installs and pip
cannot, so it sits in `/usr/lib/python3/dist-packages` where an isolated
environment cannot see it. Without the flag everything still works except
that `deckplate gui` quietly falls back to the browser. See "The
configuration page" below.

The flag has one side effect worth knowing. System versions of Pillow and
requests now satisfy the requirements, and on Mint those are older than what
this project was built against, so pip leaves them alone. Force them, and
their two HTTPS dependencies, into the environment itself afterwards:

```bash
.venv/bin/pip install --upgrade Pillow requests urllib3 certifi
```

On Windows use `.venv\Scripts\python.exe` instead, and close the official
SOOMFON app first or it swallows every report from the deck. On this machine
the environment lives outside the synced project folder, at
`%LOCALAPPDATA%\deckplate-venv`, so Windows binaries do not sync to Linux.

On Linux the deck's hidraw node is root only until the udev rule is in:

```bash
sudo ./deploy/install-udev.sh
```

Then unplug and replug the deck once.

For a clickable Deckplate on the desktop and in the menu, run
`./deploy/install-desktop.sh` from the unzipped release folder, or from the
source tree after a build. It needs no sudo and installs the program to
`~/.local/bin`. One click starts the daemon if it is not running and opens
the configuration window; the daemon carries on after the window closes and
logs to `~/.local/state/deckplate/run.log`. On Windows the same thing is
`deploy\install-desktop.ps1` from the unzipped release folder, run with
`powershell -ExecutionPolicy Bypass -File`; it installs under
`%LOCALAPPDATA%\Programs\Deckplate` with shortcuts on the Desktop and in the
Start Menu, and the daemon logs to `%LOCALAPPDATA%\deckplate\run.log`.

## Running the deck

```bash
.venv/bin/python -m deckplate run
```

The first run writes a default config and tells you where it is
(`config-path` prints it: `~/.config/deckplate/config.toml` on Linux,
`%APPDATA%\deckplate\config.toml` on Windows). Edit it in any text editor;
the daemon notices the save and applies it live. Put pictures in the
`images/` folder next to it and refer to them by relative path.

The config is documented by its own comments. In short:

- `[deck]` brightness, the idle timeout before the screens turn off,
  `press_flash`, which brightens a key for a moment when it is pressed, and
  `background`, the colour behind every key and panel that has none of its
  own (a key or strip entry may carry its own `background = "#rrggbb"`).
  `long_press_ms` and `double_press_ms` are the two thresholds for second
  actions, and `follow_focus` with `focus_poll_ms` turns on per application
  pages. Both are described below.
- `[weather]` coordinates and units. `deckplate geocode "Town"` prints
  the coordinates for a place name.
- `[strip]` the three right hand panels: `clock`, `date`, `weather`, `blank`
  or a picture path. The panels show less than the 95 pixel picture they
  are sent, so `visible_width` and `visible_height` say how much: clock,
  date and weather are drawn to fit that area, and a picture given as
  `{ image = "images/x.png", fit = true }` is shrunk to it instead of
  filling the panel with its edges cut off. Measured at about 79 pixels
  each way on the XF-CN001; `deckplate calibrate-strip` shows framed
  test tiles on the panels to check yours.
- `[[pages]]` may carry `wallpaper = "images/bg.png"`: one picture scaled to
  cover the whole key grid and cut into a slice per key, under every key that
  has no picture or animation of its own, with labels in a band on top. With
  `key_pitch_x` and `key_pitch_y` set in `[deck]` the slices line up through
  the gaps between keys, so the deck shows one continuous picture; without
  them the keys are treated as touching. On the page, the editor shows the
  page's wallpaper whenever no key is selected.
- `[[pages]]` each with `[[pages.keys]]` entries carrying `row`, `column`
  (from the top left), optional `image` and `label` (a label on its own is
  the face of the key: big, wrapped word by word at the largest size that
  fits, so "Request Hangar Access" reads on three lines; with a picture it
  sits in a band along the bottom), an optional
  `animation` such as `{ kind = "pulse", colour = "#4caf7d", speed = 1.0 }`
  with kinds pulse, spinner, wave, rainbow and scroll (an animated GIF as
  the image plays by itself, and a strip panel takes
  `{ animation = { kind = "wave" } }`), and an `action` of type
  `hotkey`, `sequence`, `launch`, `url`, `page`, `brightness`, `sleep` or
  `hold`, `multi`. A `sequence` sends several keys in order with a
  `delay_ms` pause between them, for example `keys = ["ctrl+l", "h", "enter"]`.
  A `chord` keeps one key down while tapping others, with a random pause
  before each tap between `delay_min_ms` and `delay_max_ms`; the held key
  is never let go until the last tap, for example `hold = "alt"` with
  `keys = ["1", "2", "3"]`.
  A `hold` keeps a key pressed for games that want it held: press the deck
  key once to start and again to let go. Between `hold_min_ms` and
  `hold_max_ms` later it lets go for a random moment between `release_min_ms`
  and `release_max_ms`, then presses again, so it does not look like a key
  taped down. The key shows a green mark while it is held.

### Second actions

A key may carry `action_long` and `action_double` beside its `action`, each
taking any of the same action types:

```toml
[[pages.keys]]
row = 2
column = 4
label = "Page 2"
action        = { type = "page", page = "next" }
action_long   = { type = "page", page = "Second" }
action_double = { type = "brightness", delta = -20 }
```

The long press action runs the moment `long_press_ms` passes with the key
still down, not when it is let go, so holding a key feels like it did
something; the release then does nothing more. A key with a double press
action cannot know on the first release whether a second press is coming, so
its plain action waits out `double_press_ms` before running. Two presses
inside that window are the double press, and the plain action is dropped
rather than run as well.

That waiting is the whole cost, and it falls only on keys that have a second
action. A key with neither still fires the instant it is pressed, exactly as
before, and `hold` keeps its press to toggle feel. The defaults are 400 ms
for a hold and 300 ms for a double press.

On the configuration page the two live behind "Long press and double press"
under the plain action, folded away unless the key has one. "Try the hold"
and "Try the double" appear beside "Press now" to run each one without
touching the deck.

### Per application pages

With `follow_focus = true` in `[deck]`, a page that gives itself a
`match_window` comes up by itself when a window matching it is in front:

```toml
[[pages]]
name = "Browser"
match_window = "firefox|chromium"
```

It is a regular expression, matched without regard to case against the
focused window's class, the program name and the title, and it is a search
rather than a full match, so `firefox` is enough. Pages are tried in file
order and the first match wins. When nothing matches, the deck goes to the
first page with no `match_window` of its own, so keep one ordinary page for
everything else. Switching page by hand, from a key or from the page, sticks
until the focused window changes: the deck follows the focus when the focus
moves rather than overruling you a fifth of a second later.

The focused window is read five times a second by default (`focus_poll_ms`),
on a thread of its own so the deck's own loop never waits on the desktop.
Linux needs an X11 session with `xprop` installed, which is what Cinnamon on
Mint gives you; Windows uses `GetForegroundWindow`. A Wayland session cannot
be asked which window is focused, so the daemon says so once in its log and
pages stay manual. Reading only: nothing here focuses, moves or closes a
window.

Hotkeys use the same names on both platforms (`ctrl+alt+t`, `shift+f5`,
`media_volume_mute`). They are sent with pynput, or xdotool on Linux when
pynput is not available. Programs can have a `command_windows` or
`command_linux` override beside the shared `command`, so one config file
serves both machines.

The official SOOMFON app (and OpenDeck or StreamController on Linux) fights
over the deck if both run at once. At start, `run` looks for them and asks
whether to stop them. Set `official_software` in `[deck]` to `stop` or `keep`
to skip the question, or pass `--stop-official` or `--keep-official` for one
run. Without a terminal to ask in, the other software is left alone and a
warning is printed.

Any key press wakes a sleeping deck. If the deck is unplugged the daemon
waits and reconnects.

Working on the software with no deck to hand: `FAKE_DECK_WHEN_ABSENT` at the
top of `deckplate/device.py` makes `run` drive a fake deck in memory when
none is plugged in, so the daemon and the configuration page work without
hardware. It is the real `Deck` class over a transport that goes nowhere, so
the same protocol code and the same safety gates run. The daemon says
`NO HARDWARE` in its log and the page shows "Fake deck" as the device. It is
a constant in the code on purpose, with no config key or flag, and
`tools/build.py` refuses to build while it is on. On Linux `deploy/deckplate.service` starts it with
your session; the comments in it say how to install it.

## The configuration page

With the daemon running, open the page in its own window:

```bash
.venv/bin/python -m deckplate gui
```

It uses pywebview, which wraps the browser engine the machine already has:
WebView2 on Windows 11 and WebKitGTK on Mint. On Mint install the bindings
once:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

Those are apt packages, so the environment has to have been created with
`--system-site-packages` for pywebview to find them. If the window never
appears and the page opens in the browser instead, that is almost always
why: check `include-system-site-packages` in `.venv/pyvenv.cfg`. There is no
equivalent step on Windows, where WebView2 is part of the system rather than
a Python package.

If pywebview is missing or the window cannot open, the same page opens in
the normal browser, and `gui --browser` does that on purpose. Either way it
is the same HTML at `http://127.0.0.1:8765/`, so it looks the same on both
platforms.

The page shows the deck as it really is, with the tiles the panels are
showing and a flash when a real key is pressed. Click a key to set its
label, picture and action; click a strip panel to choose clock, date,
weather or a picture. Keys for the hotkey, sequence and hold actions are
captured rather than typed: click Capture and press them. Click a captured
key to remove it. For keys the browser cannot see, such as media keys,
"type it instead" opens a text box that takes the names listed above. Drag
a key to another position to move it, or onto another key to swap them.
Double click a page tab to rename it and drag tabs to reorder them.
Uploaded pictures are kept in an `images` folder next to the config file.
Pages are tabs; double click one to rename or delete it. A panel showing the
weather has the weather place (with a search) and units in its own editor.
The cog in the header opens the settings window for what applies to the
whole deck: brightness at start, idle sleep, the deck wide background, the
strip panels' visible area, the pictures folder with what uses each picture
and a Delete for the unused ones, and where the API listens. Every change
saves a moment after you make it and the deck updates at once; Undo (or
Ctrl+Z outside a text field) steps back.

One thing to know: the page rewrites the config file from its own copy, so
comments you added by hand are not kept. The file stays hand editable
between visits and the daemon still reloads it live.

## The local API

While `run` is going it serves a small API on `http://127.0.0.1:8765/`. This
is the channel the configuration page will use, and it is handy on its own:

| Route | What it does |
| --- | --- |
| `GET /api/state` | JSON: current page, page names, keys on the page, brightness, asleep, device, weather, the key grid |
| `GET /api/tiles/ROW/COL.jpg` | the tile as shown on that panel, upright, column 5 being the strip |
| `GET /api/config` | the config file text |
| `PUT /api/config` | replace the config file; it is validated first and applied at once |
| `GET /api/document`, `PUT /api/document` | the config as JSON, the form the page edits |
| `GET /api/images`, `GET /api/images/NAME`, `POST /api/images`, `DELETE /api/images/NAME` | the pictures folder next to the config: list with what uses each, fetch, upload with the file as the body and its name in `X-Filename`, delete when unused |
| `GET /api/geocode?name=Town` | coordinates for a place name |
| `GET /api/events` | server sent events: key presses, page changes, tile updates, brightness, sleep |
| `POST /api/page` | `{"page": "Name"}` or `next` or `previous` |
| `POST /api/brightness` | `{"value": 60}` or `{"delta": -10}` |
| `POST /api/press` | `{"row": 0, "column": 0}` acts as if that key was pressed. An optional `"gesture"` of `"press"`, `"long"` or `"double"` picks which of the key's three actions to run |
| `POST /api/wake`, `POST /api/sleep` | wake a sleeping deck, or put it to sleep |

POST bodies must be `application/json` and the config PUT must be
`application/toml`. That is deliberate: browsers refuse to send those types
to another origin without a preflight, which this server never grants, so a
website open in your browser cannot drive the deck. The `Origin` and `Host`
headers are checked for the same reason.

`[server]` in the config sets `enabled`, `bind` and `port`. It listens on
this machine only unless `bind` is changed, and then a `token` is required
on every request (header `X-Token` or `?token=`). Changing `[server]` needs a
restart; everything else reloads live.

## Commands

| Command | What it does |
| --- | --- |
| `run` | drive the deck from the config file. `--config PATH` to use another file, `--seconds N` to stop after a while, `--stop-official` or `--keep-official` to skip the question about the official app |
| `gui` | open the configuration page in a window; `--browser` opens it in the normal browser instead |
| `init-config` | write the default config if it does not exist |
| `config-path` | print where the config file lives |
| `geocode "Town"` | look up coordinates for `[weather]` |
| `devices` | list known decks that are plugged in |
| `layout` | print the key grid above |
| `probe` | numbered tiles on every key, then print key events for 30 seconds |
| `listen 60` | print key events for a minute |
| `show ROW COL FILE` | put a picture on one key, for example `show 0 4 pic.png` for the top right key |
| `brightness 60` | set brightness, 0 to 100 |
| `calibrate-strip` | framed test tiles on the strip panels to measure their visible area; `--insets 6,7,9` picks the frame distances |
| `calibrate-grid --pitch 142,160` | one picture cut across all fifteen keys at that key pitch (centre to centre, across and down, in key pixels), to measure the gaps between keys: the lines run straight through the gaps when the pitch is right. Record the answer as `key_pitch_x` and `key_pitch_y` in `[deck]`. Measured at 142 by 158 on the XF-CN001. Stop the daemon first |
| `clear` | blank every key |

## Layout of the code

| Path | Purpose |
| --- | --- |
| `deckplate/protocol.py` | the wire format, pure functions, fully unit tested |
| `deckplate/layout.py` | positions to firmware key numbers and back |
| `deckplate/images.py` | resize, orient and JPEG encode pictures for the panels |
| `deckplate/device.py` | hidapi transport and the `Deck` class |
| `deckplate/config.py` | the TOML config file, validated into dataclasses |
| `deckplate/tiles.py` | pictures with labels, clock, date and weather tiles |
| `deckplate/animations.py` | built in animated effects and animated GIF playback |
| `deckplate/weather.py` | Open Meteo readings with caching, and place name lookup |
| `deckplate/hotkeys.py` | key combination names and the pynput and xdotool backends |
| `deckplate/actions.py` | runs the action behind a key |
| `deckplate/presses.py` | decides whether a press was a press, a hold or a double press |
| `deckplate/focus.py` | reads the focused window, so pages can follow the program in front |
| `deckplate/controller.py` | the daemon loop: render, events, idle sleep, config reload |
| `deckplate/conflicts.py` | finds and stops the official app or other deck software |
| `deckplate/server.py` | the local HTTP API and event stream for the configuration page |
| `deckplate/web/` | the configuration page: one HTML file, one stylesheet, one script, no build step |
| `deckplate/gui.py` | the window around the page (pywebview), with the browser fallback |
| `deckplate/cli.py` | the command line |
| `tests/` | pytest suite, no hardware needed |
| `tools/probe.py` | the original milestone 1 probe, now a wrapper around `probe` |
| `tools/build.py` | the PyInstaller build and platform zip, used by build-zip.sh on Linux and directly on Windows |
| `deploy/` | udev rule and its installer, systemd user unit |

## Building the single file program

Each platform builds its own program with PyInstaller, since PyInstaller
does not cross compile. One Python script does the work on both:

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python tools/build.py
```

It runs the tests, builds `deckplate` (or `deckplate.exe`), stages it
with an `INSTALL.txt` and, on Linux, the `deploy/` files, then writes
`Deckplate Dist/deckplate-v<version>-<platform>.zip` and mirrors the
same files into `Deckplate App/<platform>/`. The Windows build is about
23 MB and needs nothing installed on the target machine beyond Windows 11's
own WebView2 runtime for the window. The Linux build is about 60 MB because
it carries the GTK bindings for the window; PyInstaller is driven through a
spec written by the script, which limits the GTK data to the stock theme
(without that it bundled every icon theme on the machine, 1 GB of cursors).

## Packaging

`./build-zip.sh` (Linux) is the packaging convention's entry point. It runs
the tests, moves the build number in `VERSION` on, stamps it into
`pyproject.toml` and the package, calls `tools/build.py` for the Linux
program and its Dist zip and App mirror, and writes
`Deckplate Git/deckplate-v<version>-src.zip`, the full source tree as
pushed to GitHub. Pass `--no-bump` to ship `VERSION` as it stands. The
Windows zip is built on the Windows machine with `python tools\build.py`.

## Safety rule for the deck

Read `docs/hardware-safety.md` before touching a deck. In short:

The daemon only ever sends seven commands: init, keep alive, brightness,
clear, key image, commit and sleep. The deck understands others, including
a whole screen picture and a mode change, and on 10 September 2026 an
experiment with those left a deck unable to start its USB interface. Two
gates enforce the rule: the protocol module refuses to build any other
command unless the caller passes `experimental=True`, and the device layer
inspects every packet before it is written and refuses any other command
unless the deck was opened with `allow_experimental=True`. The daemon never
sets either. Only a deliberate test script should, and never against a
deck you cannot afford to lose.

## Protocol in one paragraph

USB HID, vendor interface with usage page 0xFFA0. Output reports are 1024
bytes plus a zero report id and start with the text CRT: `DIS` to init,
`LIG` for brightness, `CLE` to clear, `HAN` to sleep, `BAT` plus a 16 bit
length and key number to start an image, then the JPEG in 1024 byte chunks,
then `STP` to commit. Images are 95 by 95 JPEG, rotated 90 degrees clockwise
and mirrored on both axes before sending. Input reports are 512 bytes starting
with the text ACK; byte 9 is the key number from 1 and byte 10 is 1 for press
or 0 for release. The older CN001 revision of the same deck (USB 5548:6670)
uses 512 byte packets and 85 pixel images and is defined alongside.

Credits: the protocol was worked out by the community around the mirajazz
library and the OpenDeck akp153 plugin, and confirmed here on real hardware.

## Licence

[GPL-2.0-or-later](https://www.gnu.org/licenses/gpl-2.0.html)
