# Changelog

## 1.0.15 (19/09/2026)

- Three example configurations in `examples/`: a streaming desk, focus
  sessions, and meetings. Each is complete, uses only the shipped theme
  icons, and is drawn in the README as the deck shows it.
- `tools/render_layout.py` draws a configuration as a picture of the deck,
  for the documentation. It opens no device and needs no deck, the clock
  and the weather come from fixed values so the pictures are reproducible,
  and a key can be shown part way through a count or wearing its active
  face.
- A live key's ring is drawn under its label band rather than over it. On a
  key with a picture the ring cut straight through the time in the band and
  left it unreadable; the band is translucent, so the ring still shows
  through with the order this way round.
- Holding a stopwatch key puts it back to zero. A stopwatch with no long
  press of its own is given that reset rather than having to be configured,
  which is what the physical ones do. It costs the key its immediate press:
  like any key with a second action, its press to start now fires when the
  key comes up. A long press written by hand still wins.
- The action for a key is chosen from a window of tiles rather than a list
  of names. Each tile carries a drawing and a line saying what the action
  does, the tiles are grouped, and a search box narrows them. The same
  window picks a multi action's steps, a toggle's two halves and a timer's
  done action, where it offers only the types that may sit inside another
  action. There were twenty one names in that dropdown and nothing to say
  what any of them did.

## 1.0.14 (19/09/2026)

- Seven more action types. `toggle` alternates between two actions;
  `timer`, `stopwatch` and `counter` keep their state on the key and show it
  there, the timer and stopwatch with a ring that empties or sweeps, the
  timer running a `done` action once when it reaches zero; `volume` sets,
  steps or mutes the sound; `audio_output` switches the default output by
  name or to the next one; `window` brings another program's window to the
  front, or minimises, maximises or closes it, launching the program when
  there is no window. Linux drives the sound through pactl and windows
  through xdotool; Windows uses pycaw and the AudioDeviceCmdlets module for
  sound and user32 for windows, and falls back to the media keys for volume
  changes and mute when pycaw is absent.
- A key has another face: `image_active` and `label_active`, shown while
  its toggle is on, its hold, boost or repeat runs, its timer or stopwatch
  goes, the sound is muted by it, or its output is the one in use. On the
  page they sit behind "Picture and label while active".
- A second icon theme, "Controls": twenty nine pictures for the deck's own
  actions and four animated ones, a running hourglass, a running stopwatch,
  an emptying ring and a tally counting up. Themes may now hold animated
  GIF icons, which play on the key like any animated picture, and the icon
  tool draws both sets with `--set`.

## 1.0.13 (19/09/2026)

- Choosing "Launch a program" on a key left the page saying not saved until
  a command was typed, because the parser refused an empty command while
  every other type accepted its half filled in shape. An empty command now
  loads, and the key does nothing until one is given.

## 1.0.12 (19/09/2026)

- Theme icons that the icon tool wrote straight into an images folder before
  themes existed, as `sc-<id>.png`, were being treated as uploads: every glyph
  appeared in the picture dropdown beside the gallery and in the pictures list
  with a Delete button. A theme's manifest now declares the prefix the tool
  used, the daemon keeps any such file out of the upload list and reports it
  as the theme icon it is, and the page shows a key that still names one as a
  theme icon. The files stay where they are and keys that use them draw as
  before; theme icons are chosen from the gallery only.

## 1.0.11 (19/09/2026)

- Two new action types. `text` types a piece of text into whatever has
  focus, as written rather than parsed as key names, with an optional enter
  afterwards and an optional pause between characters for programs that drop
  fast typing. `request` calls a web address without a browser, with a
  method, a body, headers and a token read from a file outside the config
  folder on every press, so a key can reach anything with an HTTP API. Both
  are on the configuration page and in the multi action's step menu. Nothing
  here touches the deck: both live on the PC side of the action runner.

## 1.0.10 (19/09/2026)

- The comment tags outside Python are in the order the house style sets. The
  order was being checked mechanically for Python only, so five blocks in the
  other languages kept the wrong one: the parameter sat after the remarks in
  both desktop installers, and in the configuration page script one block put
  the exception before the remarks and two more put the parameter after it.
  Order only, no wording changed.
- The ignore lines for the pre rename Soomfan output folders are gone now the
  folders themselves have been archived, as the comment above them asked.

## 1.0.9 (19/09/2026)

- Every file, class and function in the tree now carries a doc comment in the
  house style: a summary, then the parameters, what comes back, the remarks
  and any exception. The tree was partway there, with the newer files carrying
  parameters and returns but no summary anywhere and the older ones carrying
  prose or nothing, and a half converted file is worse than either.
  What went into the remarks is the part that cannot be read off a signature:
  that the firmware numbers keys down each column starting from the right, so
  treating a key number as reading order misplaces every picture and looks
  like a drawing fault; that pictures are squashed to a square without keeping
  their aspect; that the build script is not idempotent and burns a version on
  every run; and that the three checks the local API makes on a request are
  the whole defence rather than a formality.
- Ninety three small private helpers deliberately carry no comment. A summary
  that only restates the name makes a file worse.
- `deckplate --help` no longer prints comment tags at the reader. Three files
  hand their own docstring to the argument parser as the help description, so
  documenting them to the house style put a summary tag at the top of the
  help. The tags are now stripped on the way through, and a cross reference
  written inside a sentence is unwrapped to the bare name.
- Three comment blocks left unterminated by the earlier partial pass are
  closed, and one that sat above the wrong function is moved onto the right
  one.

## 1.0.8 (19/09/2026)

- Two new actions for flying, both switched on and off by pressing the deck key
  again, and both marked green on the key while they run:
  - "Hold a key and pulse another": one key is held down the whole time while a
    second is pressed for `on_ms` and let go for `off_ms`, over and over.
    Forward and then boosting. Defaults 10 seconds on, 12 off, each on a slider.
    Both keys are released when it stops, or if anything goes wrong.
  - "Press a key over and over": taps a key, waits `every_ms`, taps it again,
    until it is switched off. Default 30 seconds, on a slider up to 5 minutes.
  Neither may be a step of a multi action, for the same reason a hold may not.
- "Send a hotkey" can now be held down rather than tapped: a `hold_ms` slider
  from a tap up to 10 seconds, for a game that wants the key held.
- The picture that spans every key, a page's wallpaper, offers uploads only. The
  built in icon themes belong on a single key or strip panel, not stretched
  across the whole deck.
- The two kinds of running key read differently on the deck. A key holding
  something down (hold, boost) keeps a steady border while it is engaged. A
  repeat is not holding anything, it fires now and then, so its border blinks
  while it runs. Only the keys whose border actually changed are redrawn, so a
  blink costs one small picture twice a second, not the whole page, and the
  border clears the moment the task is switched off.
- That border is set per key, under the action in the editor: `mark = { style,
  colour, flash_ms }`. Style is "auto" (blink a repeat, hold a steady border
  for a hold or a boost), "steady", "blink" or "none" to leave the key alone.
  The colour is the key's own, defaulting to the green a running key has always
  used, and flash_ms sets how long each half of that key's blink lasts, so two
  keys can blink at different rates. The loop shortens its own wait to keep up
  with the fastest blink running. A key that never touches any of it writes
  nothing to the config file.
- The deck grid on the configuration page behaves itself:
  - A key can be dragged onto an empty space, not just swapped with another.
    The drag now puts something on the transfer, which engines such as
    WebKitGTK require before they will start one at all, and the key's picture
    no longer drags on its own and carries the image instead of the key.
  - Getting back to nothing selected, which is where the page settings and the
    wallpaper live, is no longer a guess: a "Page settings" button sits at the
    top of the key editor, Escape does it, and so does clicking the empty space
    around the keys. Clicking the selected key again still works.
  - Only ever one key is marked. A rebuilt grid puts the selection back, and a
    drag let go outside the grid no longer leaves an outline behind that read
    as a second selected key.
- Four more icons in the Space Game theme: helmet, scan loop (the scan sweep
  with an arrow looping over it), landing lock (the gear with a padlock) and
  view change (a screen with cycle arrows). Twenty four in the set.
- Icon themes. A theme is a set of built in key pictures shipped with the app,
  under `deckplate/themes/<slug>/` as a `manifest.json` and one PNG per icon. A
  key refers to one as `theme:<slug>/<id>`, which the config resolves to the
  packaged file. The picture picker gains a "Choose from a theme" button that
  opens a searchable gallery grouped by theme, kept apart from the user's own
  uploads: type to find an icon by name, then click to put it on the button. The
  first theme is "Space Game", the twenty space sim glyphs. New endpoints
  `GET /api/themes` and `GET /api/themes/<slug>/<icon>.png`, both safe against
  path traversal.
- `tools/make_icons.py` draws a set of twenty flat key pictures for space sim
  actions (hangar, landing gear, quantum drive, power, engines, shields,
  weapons, missiles, scan, star map, mobiGlas, comms, cargo, mining,
  medical, exit, lights, cruise, decoupled, inventory) in the page's own
  palette on a transparent background. It writes them as sc-<name>.png into a
  folder, or with `--theme DIR` as a shipped theme (bare names and a manifest).
  Plain glyphs, no game artwork.
- The editor and tooling state is kept out of the repository and both zips by
  the git exclude file alone. It was also being excluded by name in the ignore
  file and again in the build script, and both of those ship inside the source
  zip, so naming it there published the very names the exclusion existed to
  keep back. The build reads the exclude file at build time and checks each
  staged tree afterwards, so the zips still match what git ignores and a
  pattern that quietly stopped working stops the build rather than shipping.

## 1.0.7 (16/09/2026)

- Wallpaper: a page may carry `wallpaper = "images/bg.png"`, one picture
  scaled to cover the whole key grid and cut into a slice per key at the
  measured key pitch, so the deck shows it as one continuous picture through
  the gaps. It sits under every key that has no picture or animation of its
  own; labels go in a band on top, an action only key keeps its mark, and a
  key with its own picture keeps that. The page editor shows the wallpaper
  picker whenever no key is selected, and its grid gaps follow the pitch so
  the picture reads through them there too. A picture in use as a wallpaper
  cannot be deleted from Settings.
- Fixed: the picture list in Settings was fetched once when the page loaded
  and after an upload, so after a key let go of a picture it still showed as
  in use with Delete greyed out, and a picture set on a key since could show
  as free. The list is fetched again whenever Settings opens and after every
  save while it is open.

## 1.0.6 (16/09/2026)

- `calibrate-grid --pitch 142,160`: one picture cut across all fifteen keys at
  a guessed key pitch, to measure the gaps between keys. Diagonals, a circle
  and centre lines run straight through the gaps when the pitch is right and
  step at each gap when it is not. The answer goes in `[deck]` as
  `key_pitch_x` and `key_pitch_y`, in pixels of a key image, unset until
  measured; 142 by 158 on the XF-CN001. Groundwork for splitting one picture over every key so it reads
  as continuous through the gaps.

## 1.0.5 (15/09/2026)

- Page: animated keys and strip panels now move on the page as they do on
  the deck. New route `GET /api/tiles/<row>/<col>.gif` returns the whole loop
  as a GIF, drawn and encoded for the page only, thinned to 90 frames and
  cached until the panel changes. The state and every tiles event carry the
  animated positions so the page asks for a loop only where there is one.
- Scroll effect: the text now starts centred instead of parked off the right
  edge, so the first frame reads, on the deck the instant a page comes up
  and in any still preview. Before this a scrolling key showed as empty on
  the page.

## 1.0.4 (15/09/2026)

- Label only keys: a label with no picture is now the face of the key, drawn
  big and wrapped word by word at the largest size that fits, centred, so
  "Request Hangar Access" reads on three lines instead of shrinking into the
  band at the bottom. Short labels stay on one line rather than splitting
  for bigger print. A picture with a label keeps the band.

## 1.0.3 (15/09/2026)

- Fixed: a deck woken from the window after an idle sleep went straight back
  to sleep. The wake did not reset the idle clock, so the next tick saw the
  deck idle past its limit. Seen as a scrolling label that never moved. Any
  wake now counts as activity.

## 1.0.2 (15/09/2026)

- Windows desktop entry: `deploy/install-desktop.ps1` installs the program
  under `%LOCALAPPDATA%\Programs\Deckplate` with shortcuts on the Desktop
  and in the Start Menu, no admin. A click runs `deploy/deckplate-launch.ps1`
  hidden, which starts the daemon if nothing is listening and opens the
  window, as the Linux launcher does. The exe now carries the Deckplate icon,
  drawn as `deploy/deckplate.ico` by `tools/make_icon.py` from the same
  geometry as the SVG. Written on Mint; tested on the Windows machine.

## 1.0.1 (14/09/2026)

- Linux desktop entry: `deploy/install-desktop.sh` installs the program to
  `~/.local/bin` with a Deckplate icon on the desktop and in the menu, no
  sudo. A click runs `deploy/deckplate-launch.sh`, which starts the daemon
  detached if it is not already listening, then opens the configuration
  window; the daemon carries on after the window closes and logs to
  `~/.local/state/deckplate/run.log`. The four deploy files ship in the
  Linux zip and INSTALL.txt points at them.
- Build: the Git zip is staged with .gitignore as the one exclusion list,
  after a src zip picked up untracked folders and captures.

## 1.0.0 (14/09/2026)

- Feasibility review and hardware identification (`REVIEW.md`).
- Milestone 1: hardware probe. Confirmed on a real XF-CN001 unit: tile
  orientation, key numbering, input report layout, and that the side buttons
  and display strip are separate things.
- Milestone 2: `deckplate` package with protocol, layout, images and device
  modules, a command line, a pytest suite that needs no hardware, the udev
  rule with installer, and the packaging convention scaffolding (`VERSION`,
  `.gitignore`, `build-zip.sh`).
- Configuration GUI options review (`docs/gui-review.md`).
- Milestone 3: the controller. TOML config with pages, pictures, labels and
  actions (hotkey, launch, URL, page, brightness, sleep, multi), clock, date
  and Open Meteo weather tiles on the display strip, idle sleep with wake on
  any key, live config reload, reconnect when the deck is unplugged, a
  systemd user unit, and `run`, `init-config`, `config-path` and `geocode`
  commands.
- `run` detects the official SOOMFON app (and OpenDeck or StreamController
  on Linux) and asks whether to stop it, with an `official_software` config
  setting and `--stop-official` and `--keep-official` flags.
- Milestone 4: the local HTTP API on 127.0.0.1:8765. State, tile images,
  config read and write with validation, page, brightness, press and wake
  commands, and a server sent events stream. Standard library only. The
  controller gained an event hub, a command queue and a tile cache. Hardened
  after review: Origin, Host and content type checks keep other websites
  out, config saves are byte exact and atomic, the event stream ends cleanly
  on shutdown and on deck loss, and request bodies are always consumed.
- Milestone 5: the configuration page. Served by the daemon, plain HTML,
  CSS and JavaScript with no build step, identical on Windows and Linux.
  Live deck view with real tiles and key flashes, page tabs, a key editor
  for label, picture and every action type including multi step, a strip
  panel editor, settings with a weather place search, page management,
  undo, and saves applied live. New API routes for the config as JSON,
  picture upload and place lookup, plus a TOML emitter. The `gui` command
  opens the page in a pywebview window with a browser fallback.
- Sequence action: several keys in order with a pause between them, set by
  a slider on the page, and a capture mode that appends every key pressed.
  Key combinations in the config are now checked at load time.
- Hold action: press the deck key once to hold a keyboard key down, again
  to let go, with two dual sliders for how long to hold and how long to let
  go; each cycle picks a random time in the range. Held keys are released
  on reload and at exit, and the tile shows a green mark while holding.
- Milestone 6: single file builds. `tools/build.py` runs PyInstaller on
  either platform, stages the program with an INSTALL.txt (and the deploy
  files on Linux), writes the platform Dist zip and mirrors it into the App
  folder. build-zip.sh calls it for the Linux zip. The Windows build was made
  and checked on the Windows machine. The config loader now accepts a byte
  order mark, as left by Notepad and PowerShell.
- Page: keys are captured, not typed. Hotkey, sequence and hold show the
  captured keys as chips with Capture and Clear, a chip is removed by
  clicking it, and a "type it instead" link opens a text box for keys the
  browser cannot see. The page no longer rebuilds the editor when its own
  save comes back from the daemon, which was breaking multi key capture and
  stealing focus while typing. Dropped API connections are no longer logged
  as tracebacks.
- Chord action: hold one key while tapping a key or a sequence, with a dual
  slider for a random pause before each tap. The held key stays down for
  the whole run and is always released afterwards, even if a tap fails.
  A chord with nothing captured yet saves cleanly, and the picture picker
  no longer shows a broken image icon when a key has no picture.
- A modifier tapped on its own is captured, for the chord's held key. A
  page switch or brightness change from the window wakes a sleeping deck.
- Page: pages are renamed by double clicking a tab and reordered by dragging
  tabs (or with arrows in Settings), keys are moved or swapped by dragging
  them between positions, and the page's own dialog replaces the browser's
  prompt and confirm boxes.
- Strip panels: they show less than the picture they are sent, so `[strip]`
  gains `visible_width` and `visible_height`, clock, date and weather are
  drawn to fit that area, a picture can be shrunk to it with `fit = true`,
  the deck view shades the cut off edges, and `calibrate-strip` shows
  framed test tiles on the panels to measure the real values. Measured on
  the XF-CN001 at about 79 pixels each way; the default is 78.
- Animations. An animated GIF set as a key's picture plays by itself, and
  five built in effects can be drawn in place of a picture on a key or a
  strip panel: pulse, spinner, wave, rainbow and a scrolling label, with a
  colour and speed. Frames are rendered once and played by time, so the
  loop runs faster only while something is animating. A pressed key
  flashes briefly (`press_flash`, on by default). Effects run at 20 frames
  a second (up to 30) after the first cut looked steppy at 8.
- Background colour: each key and strip panel can have its own, with a
  "Set to default" button, and Settings sets the colour for the whole deck
  with its own reset to the original navy (`[deck] background` and a
  `background` field on keys and strip entries).
- Removed again the same day: painting the screen behind the keys with
  the whole screen picture command. On this firmware that command paints
  only part of the screen, stores whatever it is given as the boot
  picture, and together with a mode command it left a deck unable to start
  USB. The protocol module now refuses every command outside the proven
  set of DIS, CONNECT, LIG, CLE, HAN, BAT and STP unless a caller marks it
  experimental, so the daemon cannot send one by accident. The white
  surround after a USB drop remains an open question for a USB capture of
  the official app.
- Fixed: the hold action crashed on Python 3.12 and older when a hold was
  released, because its stop flag shadowed an internal method of the thread
  class. Found running the suite on Mint.
- Development: `FAKE_DECK_WHEN_ABSENT` in `device.py` drives a fake deck in
  memory when no deck is plugged in, so the daemon and the page can be
  worked on without hardware. Code only, no config or flag; the build
  refuses while it is on.
- Linux build made on Mint. PyInstaller is now driven through a spec so the
  GTK data can be limited to the stock theme: the first Linux build carried
  every icon and GTK theme on the machine, 440 MB and 11 seconds to start;
  now 60 MB and under a second.
- Page: drop down fields draw their own arrow and declare the dark colour
  scheme, so WebKitGTK on Linux no longer paints them white from the desktop
  theme.
- Page: Settings is a cog in the header that opens a window, and the window
  holds only what applies to the whole deck. The weather place and units
  moved into the editor of a strip panel that shows the weather, and a page
  is deleted from its rename dialog now that the page list is gone.
- Pictures: Settings lists every uploaded picture with a preview, its size,
  which keys use it, and a Delete button for the ones nothing uses. The
  folder path is shown. New API route `DELETE /api/images/<name>`.
- Second actions: a key may carry `action_long` and `action_double` beside
  its `action`, with `long_press_ms` and `double_press_ms` thresholds in
  `[deck]`. The long press runs while the key is still down; a key with a
  double press waits out the window before its plain action. Keys with
  neither still fire on the press, unchanged. New `deckplate/presses.py`
  holds the timing, the page edits all three behind a fold, and `/api/press`
  takes a `gesture` so each can be tried without the deck.
- Per application pages: `follow_focus` in `[deck]` with a `match_window`
  pattern on a page brings that page up when a matching window comes to the
  front, falling back to the first page without one. A page chosen by hand
  stays until the focused window changes. New `deckplate/focus.py` reads the
  focused window through xprop on X11 and GetForegroundWindow on Windows, on
  a thread of its own; a Wayland session is reported once and left alone.
- Surround: the keys are regions of one screen and the surround is the part
  no tile covers, which can come back light after a USB drop. A capture of
  the official app shows it clears every key with CLE on connect before it
  redraws, so start and wake now do the same with the proven clear command
  before the tiles are drawn on top.
- The replacement deck is here, so `FAKE_DECK_WHEN_ABSENT` is off. With no
  fake, an unplugged deck makes the run loop wait and reconnect to the real
  hardware when it returns instead of switching to a fake that hid the
  disconnect. The release build packages again.
- Page: stays live across a deck unplug and replug. The events route used
  to answer 503 with no deck, which a browser EventSource treats as fatal,
  so after a replug the page never updated again. One stream is now held
  open across disconnects: a waiting event, then a fresh hello when the
  deck comes back.
- USB capture tooling: `tools/capture-deck.bat` records the official app's
  traffic with USBPcap on Windows and `tools/decode_capture.py` lists every
  command it sent and reassembles the images, reading pcap files only.
  Captures are ignored by git and left out of both zips.
- Auto sleep defaults to never: `sleep_after_minutes` is 0 in a fresh
  config, and any value from 1 to 1440 in Settings turns the idle timeout
  back on.
- Page: buttons are drawn dark on Linux. WebKitGTK painted its native light
  button over the styled one because appearance was never reset, as the
  selects already did.
