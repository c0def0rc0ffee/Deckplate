# Configuration GUI: Options Review

Date: 10 September 2026
Status: the recommendation below was built the same day as milestone 5. The
page lives in `deckplate/web/` and the window in `deckplate/gui.py`.

## The ask

A nice GUI whose only job is configuring the deck: which picture and action
goes on which key, what the display strip shows, brightness and the like. It
should look the same on Windows and Linux Mint, or as close as a reasonable
effort gets. It does not need to be a media player, a soundboard or a plugin
marketplace. The official app bundles all of those, including a music player,
which is a large part of why it is slow and crashes.

## Where the GUI sits

The daemon owns the deck. One process holds the USB device, renders tiles,
watches the clock and the weather, and runs actions when keys are pressed.
The GUI never talks to the hardware. It edits the config and asks the daemon
to reload, and it asks the daemon for live state so the on screen grid can
light up when a real key is pressed and show exactly what each panel shows.

That separation decides most of the design. Whatever toolkit the GUI uses, it
needs a channel to the daemon anyway, so the daemon has to expose one.

```
+-------------+   config file (TOML)   +-------------------------------+
|  GUI        | <--------------------> |  deckplate daemon          |
|  (config)   |   local HTTP + events  |  device, renderer, actions    |
+-------------+ <--------------------> +---------------+---------------+
                                                       | USB HID
                                                  +----+----+
                                                  |  deck   |
                                                  +---------+
```

## The candidates

| Option | Same look on both | Size of a built app | Effort | Notes |
| --- | --- | --- | --- | --- |
| Local web page served by the daemon, shown in a small native window (pywebview) | Identical by construction, it is the same HTML | Tiny. The page ships inside the package | Low to medium | Uses the browser engine already on the machine: WebView2 on Windows 11, WebKitGTK on Mint |
| Same web page, opened in the normal browser | Identical | Tiny | Lowest | No window of its own, feels less like an app |
| PySide6 (Qt 6) with the Fusion style | Very close. Fusion is Qt's own cross platform look; fonts and DPI still differ a little | Large. Around 60 to 80 MB zipped, 150 MB or more unpacked | Medium | Proper desktop toolkit, drag and drop, tray icon, file dialogs. LGPL |
| Tkinter with a ttk theme forced on both platforms | Close if one theme is forced, dated otherwise | Small, in the standard library | Medium | Drag and drop needs an extra package, image handling clumsy, hard to make it look nice |
| Flet (Flutter rendered from Python) | Identical | Large | Medium | Young project, own widget set, heavy runtime |
| Dear PyGui, Kivy | Identical | Medium | Medium to high | Non native look on both, game style rendering, harder to make it feel like a settings app |
| GTK 4 and libadwaita | Excellent on GNOME, out of place on Cinnamon, painful on Windows | Large on Windows | High | Not a fit for Mint plus Windows |

## Recommendation

Serve the configuration page from the daemon and show it in a pywebview
window. Fall back to the system browser if the window cannot open.

Why this one:

- It is the only option where "looks the same" is guaranteed rather than
  approximated. Same HTML, same CSS, same fonts bundled with the page.
- The daemon needs a local channel for the GUI anyway. Making that channel
  HTTP on 127.0.0.1 means the page is the GUI: no second protocol, no second
  process to keep alive, no separate packaging for the GUI.
- It matches how you already work. Your other projects are web projects with
  HTML, CSS and JavaScript, so the page is yours to restyle without learning a
  widget toolkit.
- The built app stays small. Pictures, drag and drop, previews and colour
  pickers are all things a browser does well without any extra dependency.
- Bonus: with a config switch it can bind to the LAN instead of localhost, so
  the deck can be configured from the phone. Off by default.

Runtime needs are modest. Windows 11 already has the WebView2 runtime. Mint
needs the GTK WebKit bindings, which are one apt line and go into the udev
installer's sibling script:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

If the pywebview window ever proves fragile on a machine, the "open in the
browser" fallback is the same page and loses nothing.

Runner up: PySide6 with the Fusion style. Choose it instead if you decide the
app must be a classic desktop program with no browser engine involved. It
costs a much bigger download and a second codebase style, and the daemon
still needs the HTTP channel for live state.

## What the page would contain

One window, two panes, the way every deck configurator is laid out.

Left pane, the deck:

- A grid drawn exactly like the hardware: five columns of three keys, then
  the display strip as a visibly different sixth column with no press
  affordance. Real key presses highlight the matching cell.
- Page tabs above the grid. Each page is a full set of 15 key assignments.
  The strip is shared across pages by default, since it holds the clock and
  weather.
- Each cell shows the actual rendered tile, the same 95 by 95 image the
  daemon sends, so what you see is what the panel shows.

Right pane, the selected key:

- Picture: drop an image file onto the cell or pick one. Optional text label
  drawn over it, with a small set of label positions and a colour.
- Action: one of hotkey, launch program, open URL, switch page, brightness,
  sleep, or nothing. Multi step actions as an ordered list. Hotkeys captured
  by pressing the combination in a field, recorded as key names so the same
  config works on both platforms.
- For strip panels: tile type of clock, date, weather or picture, with the
  weather location and units in Settings.

Settings:

- Brightness, sleep after idle, weather location (from a place name search
  against Open Meteo's geocoding, no key needed), units, start with session,
  LAN access toggle.

Behaviour:

- Every change is saved to the TOML file and applied live. No save button,
  but an undo for the last change.
- The config file remains hand editable. The page is a convenience, not the
  only way in.

## Technical shape

- Server: Python standard library `http.server` is enough for a local page,
  but the daemon also needs a push channel for key presses and tile updates.
  Server sent events over one long lived request does that with no extra
  dependency. If it gets awkward, `aiohttp` is a small, well kept addition.
- Page: plain HTML, CSS and JavaScript in `deckplate/web/`, no build step
  and no Node. A few hundred lines. If it grows into a real front end later,
  Vite can be added and the packaging convention's Vite folder with it, but
  starting without one keeps build-zip.sh as it is today.
- Window: `pywebview` in the `deckplate gui` command. Opens the daemon's
  page in a native window sized for the layout. Tray icon later if wanted.
- Rendering previews: the daemon already produces every tile as JPEG bytes,
  so the page just fetches `/tiles/<page>/<key>.jpg` and shows it. One code
  path for the screen and the panel.
- Security: bind 127.0.0.1 only unless the LAN toggle is on. No auth on the
  loopback. With LAN on, a simple shared token in the config.

## Packaging under the convention

- No Web folder. The page is served by the daemon from inside the package,
  not by Apache, and it is not a web site. The App folder stays the mirror.
- No Vite folder unless a build step is introduced, per the point above.
- Dist zip gains `deckplate/web/` and the Mint apt line in the deploy
  notes. Windows needs nothing extra on Windows 11.
- PyInstaller builds on each platform later, as already planned. pywebview
  packages cleanly with it.

## Risks

1. WebKitGTK on Mint occasionally lags behind on newer CSS. Keep the page's
   CSS conservative and it is a non issue. The browser fallback exists anyway.
2. Hotkey capture in a web page cannot see keys the browser reserves. The
   field records what it can and offers a plain text fallback for the rest.
3. Drag and drop from the file manager into a pywebview window works on
   WebView2 and WebKitGTK, but the Linux side has had bugs in the past. A
   pick file button sits beside it regardless.
4. Live state over server sent events is one request that never ends. Some
   proxy setups interfere with that, which does not apply on loopback.

## Suggested order

1. Milestone 3 as planned: daemon with TOML config, pictures, clock and
   weather tiles, actions, pages. This is the thing the GUI configures and it
   has to exist first.
2. Milestone 4: HTTP channel in the daemon (state, tiles, config read and
   write, events).
3. Milestone 5: the page, then the pywebview window and the `gui` command.
4. Milestone 6: platform builds with PyInstaller and the convention checks on
   Mint.
