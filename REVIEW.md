# Deckplate 15 Button: Feasibility Review

Date: 7 September 2026
Status: review only, no code written yet. Device identity confirmed from the
Windows machine on the same day (see below).

## Verdict

Yes. A replacement for the official software is realistic, and the hard part
(the USB protocol) has already been reverse engineered by the Linux community.
The device is a rebadged Mirabox Stream Dock 293S, a family that OpenDeck and
StreamController already drive on Linux. We can either use one of those as is,
or write a small purpose built app that does only what you asked for: pictures
on keys, a weather tile, a clock, hotkeys and app launching.

My recommendation is a two step approach:

1. Prove the hardware first with OpenDeck and its akp153 plugin (no code, about
   ten minutes). This confirms the image orientation and that the keys report
   presses. It also gives you a working deck immediately.
2. Then build our own lightweight app in Python for Linux Mint, so you are not
   carrying a large general purpose program and its plugin marketplace for
   five features.

## What the device actually is

Confirmed by reading the plugged in deck on the Windows machine:

| Item | Finding |
| --- | --- |
| Marketed name | SOOMFON Stream Controller, 15 macro keys and dashboard |
| USB ID | 1500:3003, firmware revision 0002 |
| Variant | Soomfon Stream Controller XF-CN001 (the newer revision, not the older CN001 at 5548:6670) |
| Serial number | 8730DB783520, a real per unit serial |
| USB descriptor string | "HOTSPOTEKUSB HID DEMO" (a placeholder left in the firmware, harmless) |
| Interface 0 | Vendor defined HID, usage page 0xFFA0. This is the control channel for images and key events |
| Interface 1 | A standard HID keyboard. The deck can type keystrokes on its own, most likely how the 3 side buttons work |
| Keys | 15 LCD keys in a 5 by 3 grid, plus 3 fixed side buttons |
| Connection | USB C on the deck, USB A on the host |
| Official software | "SOOMFON Control Deck", downloaded from sm.key123.vip |
| Official OS support | Windows 7 and later, macOS 10.15 and later. No Linux. |
| Real manufacturer | Mirabox. The same hardware family ships as Mirabox HSV293S (Stream Dock 293S), Ajazz AKP153, Mars Gaming MSD ONE, Womier D15, Streonor S15 and others |

Evidence for the rebadge: the OpenDeck akp153 plugin lists "Soomfon Stream
Controller XF-CN001" at 1500:3003 (added in plugin version 0.9.4, November
2025) alongside the older "Soomfon Studio Control Deck" at 5548:6670. The
download domain key123.vip is Mirabox's own creator portal, and the SOOMFON
plugin list includes a "DevOps for StreamDock" entry that only makes sense for
Mirabox's StreamDock software wearing a different badge.

For reference, the third Soomfon deck, the SE CN002 at 1500:3001, is a
different family with no Linux support yet. That is not what you have.

The keyboard interface is worth a note. Because the deck enumerates as a
keyboard as well, Windows and Linux both accept keystrokes from it without any
driver. If the side buttons turn out to send fixed key codes over that
interface, we can still bind them on Linux by remapping those key codes rather
than through the deck protocol.

## Why doubting the official software is fair

- It is closed source, Windows and macOS only, and Mirabox has said it has no
  plans to document the protocol. Its Linux SDK ships the USB layer as a
  precompiled binary rather than source.
- Independent reviews report crashes, a poor translation and the deck freezing
  for several seconds whenever you switch windows inside the software.
- SOOMFON's own release notes record crash fixes for basic operations such as
  copying a page and changing the language.
- The three side buttons are fixed function in the official software and cannot
  be reassigned.

I did not find evidence of telemetry or account harvesting, so I am not
claiming that. The case against it is simply that it is closed, unstable and
does not run on your main machine.

## The protocol, as far as it is known

All of this comes from the mirajazz Rust library (MPL 2.0) and the Python fork
used by StreamController. It is enough to write a driver from scratch.

- Transport is USB HID output reports on the hidraw interface. This deck is
  protocol version 3 in mirajazz terms: 1024 byte packets, a real serial
  number, STP required after image uploads, and separate press and release
  events for every key. The older CN001 revision is protocol version 1 with
  512 byte packets; supporting both is a small table entry.
- Confirmed from the deck's own HID report descriptor (read on the Windows
  machine): the vendor interface uses usage page 0xFFA0, one 512 byte input
  report and one 1024 byte output report, no report ids. That matches the
  protocol version 3 packet size exactly.
- The reference driver is fire and forget: it never reads acknowledgements
  after commands. The only traffic from the deck is key events, and on
  Windows the firmware version query is not available.
- Probe results (7 September 2026): with the official app closed, opening the
  interface, init, brightness and pushing 18 numbered 95 by 95 JPEG tiles all
  worked first time. Rob confirmed the tiles appeared with the numbers upright,
  so the rotate 90 plus mirror both orientation from the reference driver is
  correct for this unit. Key presses and releases were captured with the
  layout above. One transient read error occurred mid capture on Windows with
  the deck still enumerated; the probe now reopens the device and carries on.
- Every command starts with a report id of 0x00 followed by the magic bytes
  `43 52 54 00 00` (the ASCII text CRT), then a three letter command:
  - `DIS` initialise the display
  - `LIG` set brightness, one byte, 0 to 100
  - `CLE` clear one key (key number plus 1) or all keys (0xFF)
  - `HAN` put the screens to sleep
  - `BAT` start a key image transfer, followed by the image byte length as a
    16 bit big endian value and the key number plus 1
  - `STP` commit pending images (required on this revision)
  - `CONNECT` keep alive
- Key images are JPEG, 95 by 95 pixels on this revision (85 by 85 on the older
  CN001), quality around 90. The image has to be rotated 90 degrees and
  mirrored on both axes before sending, because the panels are physically
  mounted rotated. Image data is sent in 1024 byte chunks, each prefixed by a
  0x00 report id and zero padded.
- Key presses arrive as 512 byte input reports. Confirmed on the real deck on
  7 September 2026: bytes 0 to 2 are the text ACK, bytes 5 and 6 are the text
  OK, byte 9 is the key number counting from 1, and byte 10 is 1 for a press
  and 0 for a release. Nothing else in the report is used.
- The plugin maps this device as 18 keys in 3 rows of 6. That is the 15 LCD
  keys plus the 3 side buttons. During the capture on 7 September 2026 every
  LCD key 1 to 15 reported press and release, and keys 16 to 18 never appeared
  on the vendor interface at all. The three physical side buttons therefore
  do not speak the deck protocol on this firmware, even though the display
  strip beside them takes images 16 to 18. The working assumption is that the
  buttons type fixed key codes through the deck's second, keyboard, interface.
  That is still to be confirmed by pressing them with a text editor focused.
- Key numbering runs down each column and the columns count from the right.
  Read off the deck by Rob with the numbered tiles showing, the top row left
  to right is 13, 10, 7, 4, 1, 16, and the sweep captured earlier (1, 2, 3
  then 6, 5, 4 and so on) shows each column runs top to bottom. The full
  physical layout is therefore:

  ```
  13  10   7   4   1  |  16
  14  11   8   5   2  |  17
  15  12   9   6   3  |  18
  ```

  For LCD key n from 1 to 15: column from the left is 4 minus (n minus 1)
  divided by 3, row is (n minus 1) modulo 3. Keys 16 to 18 are the sixth
  column, top to bottom. Going the other way, the key at row r and column c
  (both from 0, left to right) is (4 minus c) times 3 plus r plus 1, and the
  sixth column is 16 plus r. Any config file we write will use plain row and
  column positions and hide this numbering completely.
- The sixth column is a display only strip, not buttons. Rob saw tile 16 on
  the far right screen that has no key under it, so the "dashboard" strip
  accepts images 16 to 18 like any other key but can never report a press.
  The three physical side buttons are separate from that strip.
- The keys are regions of one 854 by 480 screen, not separate panels. The
  screen shows white between and around the keys until something paints
  the surround, and loses it again when unplugged; the official app
  restores it on every connection. On 10 September 2026, with the official
  app stopped, a USB drop left the deck white between the keys. For a few
  hours that day the daemon painted the surround with the `LOG` command.
  That was a mistake: LOG is the boot logo, not the runtime background (see
  the next two entries), and the painting was removed the same evening. The
  daemon does not paint the surround at all now; the `[deck] background`
  colour applies to the key and strip tiles only.
- Hard lessons from 10 September 2026, in order. The official transport
  library (prebuilt in the Mirabox SDK, symbols intact) has these commands:
  DIS, CHECK, LLUM, LMOD, COLOR, CPOS, LIG, CLE, STP, HAN, CONNECT, BAT, LOG,
  BGPIC, BGCLE, LBLIG, SETLB, DELED, QUCMD, MOD. A LOG with an 854 by 480
  black JPEG painted only the right half of the screen; a LOG with a 1708 by
  960 picture was accepted and stored as the boot picture; a MOD command
  with mode digit 1 then wedged the deck. After a power cycle the deck draws
  the stored picture at once and never enumerates on USB again. The chip is
  an ArtInChip part whose boot ROM has a USB recovery mode (VID 33C3, PID
  6677); no button on the deck was found to enter it; the firmware tool in
  the official app folder is the ArtInChip upgrade tool, and the only public
  firmware on Mirabox's CDN is for their N1 model. The whole screen painting
  was removed from the daemon the same day and the protocol module now
  refuses every command outside the proven seven unless a caller marks it
  experimental. Settle the background command, if ever, with a USB capture
  of the official app on a deck that can be spared, not by trial. The board
  has two internal headers; a serial console on one of them would show the
  boot log and may allow entering USB recovery from the boot loader.
- The official app confirms LOG is the boot logo: it asks for an 854 by 480
  picture and warns the processed file must be under 512 KB. The 1.2 MB raw
  picture was ignored for size, the small JPEGs were accepted and drawn
  wrongly because the app converts the picture to another form first, and
  the 75 KB four colour JPEG was stored as the logo. The runtime background
  behind the keys is a different layer, probably BGPIC and BGCLE, format
  unknown.
- The strip panels show less than the 95 by 95 picture they are sent.
  Measured with framed test tiles on 10 September 2026: a frame 8 pixels in
  from the edge just fits, so about 79 pixels are visible each way and the
  rest is cut off at the edges. The software draws strip content into a
  78 by 78 window by default (`[strip] visible_width` and `visible_height`). On the Mirabox 293S that strip is documented as three
  80 by 80 panels; the 95 by 95 tiles were accepted, so the firmware scales
  or crops them. Worth a closer look for text sharpness later.
- This revision has a real serial number, so two decks could be told apart.
  The older CN001 revision shares one hard coded serial across all units.

## Existing Linux software that already drives it

| Project | What it is | Relevance |
| --- | --- | --- |
| OpenDeck plus opendeck-akp153 | Rust and Tauri desktop app, GPL 3, runs Elgato format plugins. Available as .deb, Flatpak and AUR. The akp153 plugin (version 0.11.1, August 2026) lists this exact deck at 1500:3003 | Fastest way to prove the hardware. Heavy for our needs but solid |
| StreamController | Python and GTK4 app on Flathub with a plugin store. Supports the older 293S through its own fork of the python elgato streamdeck library. Has weather and clock plugins already | Good feature match, but the fork covers the 293S at protocol version 1, not this 1500:3003 revision. Would need a device entry adding |
| mirajazz | Rust library behind both OpenDeck plugins. Cleanest protocol reference | Reference only, unless we write in Rust |
| Mirabox Linux Python SDK | Official, MIT licensed, but the USB transport is a precompiled .so | Avoid. Same closed layer as the official app |
| ajazz control center | Qt 6 alpha, lists the Ajazz ID 5548:6674 but not our 5548:6670 | Not ready |
| python elgato streamdeck PR 148 | Adds 293S support upstream, still unmerged | The StreamController fork on PyPI carries it |

OpenDeck would solve the problem today with zero code, at the cost of
installing a large application. It needs a udev rule to give your user access
to the hidraw device; the plugin ships one that already includes 1500:3003.

## Proposed software: what we would build

Working name: Deckplate. A small Linux daemon with a plain text config,
written in Python.

Scope, matching your request:

- Static pictures on keys, from any image file, auto resized to 95 by 95.
- A weather tile using Open Meteo (free, no API key, no account), refreshed
  every ten minutes, drawn with an icon and temperature.
- A clock tile, and optionally a date tile.
- Key actions: send a hotkey, launch a program or script, open a URL, switch
  page, set brightness. Multi step actions per key.
- Multiple pages with next and previous page keys.
- Brightness control and screen sleep after idle.
- Runs as a systemd user service so it starts with your session.

Deliberately out of scope: notes app, OBS, Twitch, Spotify, cloud sync, plugin
marketplace. If a specific integration is wanted later it is one Python file.

Technology choice and why:

- Python 3.11 or later with the `hid` package (hidapi), Pillow for rendering,
  and `requests` for weather. All in the Mint repositories or pip.
- Hotkeys through xdotool. Mint Cinnamon is still X11, so this is reliable. If
  you move to Wayland later, ydotool is the drop in replacement.
- Config in one TOML file (pages, keys, images, actions). No GUI editor in the
  first version; a tiny local web page editor can be added later if you want it,
  and that is the only part that would justify a Web folder.
- About 600 to 900 lines total. Easy to read and tweak, and no build step.

One build for Windows and one for Linux: yes, from the same source. The USB
side is identical on both, because hidapi wraps the Windows HID API and Linux
hidraw behind one interface, and the probe script in `tools/probe.py` already
runs unchanged on both. Only the "do something on the PC" side differs, and
that is kept behind one small module per platform:

| Concern | Linux | Windows |
| --- | --- | --- |
| USB access | hidraw plus a udev rule | Works out of the box, no driver |
| Send a hotkey | xdotool (X11) or ydotool (Wayland) | pynput or the keyboard package |
| Launch a program | subprocess | subprocess |
| Start with the session | systemd user service | Startup folder shortcut or Task Scheduler |
| Distribution | Dist zip with an install script, or a PyInstaller single file | PyInstaller single file exe, no Python install needed |

The Windows build is made with PyInstaller on a Windows machine and the Linux
build with PyInstaller on Mint; PyInstaller does not cross compile. Both come
out of the same repository and the same build-zip.sh, which would produce a
`deckplate-v<version>-linux.zip` and, when run on Windows under Git Bash, a
`deckplate-v<version>-windows.zip`. The Windows build must not run at the
same time as the official SOOMFON app, because the two would fight over the
device; the probe already showed the official app swallowing every report.

Architecture: one process with three parts. A device thread that opens the
hidraw device, pushes images and reads key reports. A renderer that turns each
key definition into a 95 by 95 JPEG with the correct rotation. An action
runner that executes the key's action off the device thread so a slow script
never blocks key reads. Config reload on file change.

Milestones:

1. Probe script: find the device, print raw input reports, push a numbered test
   image to every key. Confirms orientation, key numbering and side buttons.
2. Driver module plus tests against recorded packets.
3. Renderer, static pictures, clock, weather.
4. Actions, pages, config reload, systemd unit, install script.
5. Packaging per the convention below.

## Packaging plan under the standard convention

Only the parts that fit a Python desktop tool apply. Points that are web
specific are dropped and noted.

- Product name `deckplate`. Output folders `Deckplate Dist/`,
  `Deckplate Git/` and `Deckplate App/` in the project root. This is an
  application, not a web project, so App is used and there is no Web folder.
- No Vite folder, no Apache vhost, no `/var/www/html` mirror and no
  `deploy/setup-apache.sh`. Nothing is served over HTTP.
- `VERSION` at `1.0.0`, single source of truth. The build stamps it into
  `pyproject.toml` in place of the package.json step.
- `build-zip.sh` with `set -euo pipefail`: runs pytest first and refuses to
  package if red, stages with rsync into a mktemp directory, builds both zips,
  mirrors the staged runtime files into `Deckplate App/` with
  `rsync -a --delete`, supports `--no-bump`, and checks for zip and rsync up
  front. npm is not needed and will not be checked for.
- Dist zip: the Python package, `pyproject.toml`, `requirements.txt`, the udev
  rules file, the systemd unit, `install.sh`, default icons and an example
  config. Git zip: all of that plus README, CHANGELOG, VERSION, tests and
  .gitignore.
- Both zips exclude the usual: secrets patterns (`.env`, `*credential*`,
  `*password*`, `*secret*`, `*.pem`, `*.key`, `id_rsa*`, `*FTP*`), archives,
  logs, temp files, the three output folders, `.git/`, plus
  `__pycache__/`, `.venv/` and `*.egg-info/` for Python.
- The sudo step equivalent to the Apache setup is `deploy/install-udev.sh`: it
  copies the udev rule for 1500:3003 (and the older 5548:6670 sibling) into
  `/etc/udev/rules.d/` and reloads udev. Packaging never needs it.
- Git: real working tree, local user.name and user.email, tracked files checked
  against the Git zip contents, no commit or push without your say so.

## Risks and unknowns

1. Side buttons are silent on the vendor interface. If they type key codes
   through the keyboard interface they can be remapped on Linux; if they send
   nothing at all they stay unused. Neither outcome blocks the project.
2. On Windows, hidapi raised one transient read error in each capture session
   with the deck still enumerated. The probe reopens the device and continues,
   and the final app must do the same. Not yet seen on Linux, where hidraw is
   generally better behaved.
3. Image orientation and the key report layout are now confirmed on this unit.
   Other firmware batches could differ; the numbered test image makes that a
   two minute check.
4. Wayland would break xdotool hotkeys. Not an issue on current Mint.

## Next steps

1. Milestone 1 is done: the probe drives the deck, the tiles show correctly
   and the key reports are decoded. The side buttons were tested with a text
   editor focused and typed nothing, so they are firmware only and unused.
2. Milestone 2 is done: the `deckplate` package (protocol, layout, images,
   device, command line) with a 28 test pytest suite that needs no hardware,
   the udev rule and installer, and the packaging scaffolding (`VERSION`,
   `.gitignore`, `build-zip.sh`, git repository with a local identity).
   `build-zip.sh` has had a syntax check only; its first real run needs Mint,
   where zip and rsync exist.
3. Milestone 3 is done: the controller with a TOML config, pictures and
   labels on keys, clock, date and Open Meteo weather on the strip, all
   action types, idle sleep, live config reload and reconnect. Verified on
   the deck from Windows on 10 September 2026.
4. A configuration GUI is reviewed separately in `docs/gui-review.md`. The
   recommendation is a page served by the daemon on localhost, shown in a
   small native window, so it looks identical on Windows and Mint.
5. Milestones 4 and 5 are done: the daemon serves the local API and event
   stream, and the configuration page on top of it edits everything live.
   Verified in a browser against the real deck on 10 September 2026.
6. Milestone 6: `tools/build.py` makes the single file program on either
   platform. The Windows build (about 23 MB) was made on 10 September 2026
   and checked end to end: it found the deck, served the bundled page and
   drove the hardware. Still to do on Mint: install the dev requirements,
   run `./build-zip.sh` for the Linux zip and the Git zip, and do the point 8
   checks (zip listings, no secrets, App folder diff against the Dist zip,
   `git ls-files` against the Git zip).

## Sources

- SOOMFON product page: https://soomfon.com/products/soomfon-studio-control-deck-with-15-macro-keys-dashboard
- SOOMFON manual (model CN001, download links): https://manuals.plus/asin/B0CTT4BBYJ
- SOOMFON plugin list: https://soomfon.com/blogs/stream-controller/stream-controller-plugin-list
- SOOMFON software release notes: https://soomfon.com/blogs/soomfon-stream-controller-software-release-notes
- Review of the official software: https://mein-mmo.de/en/i-bought-a-cheap-stream-deck-as-an-alternative-to-elgato-and-immediately-regretted-it,1222098/
- OpenDeck: https://github.com/nekename/OpenDeck
- OpenDeck akp153 plugin (device table, udev rules, changelog): https://github.com/4ndv/opendeck-akp153
- Soomfon SE CN002 unsupported issue: https://github.com/4ndv/opendeck-akp03/issues/21
- mirajazz protocol library: https://github.com/4ndv/mirajazz
- StreamController: https://flathub.org/en/apps/com.core447.StreamController
- StreamController streamdeck fork: https://github.com/StreamController/streamcontroller-python-elgato-streamdeck
- Upstream 293S pull request: https://github.com/abcminiuser/python-elgato-streamdeck/pull/148
- Mirabox official device SDK: https://github.com/MiraboxSpace/StreamDock-Device-SDK
- Node protocol example: https://github.com/rigor789/mirabox-streamdock-node
- Ajazz control center: https://github.com/Aiacos/ajazz-control-center
