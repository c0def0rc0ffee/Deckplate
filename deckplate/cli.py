"""<summary>
Command line entry point.

    python -m deckplate run              drive the deck from the config file
    python -m deckplate gui              open the configuration page in a window
    python -m deckplate init-config      write the default config if missing
    python -m deckplate config-path      print where the config file lives
    python -m deckplate geocode "Town"   look up coordinates for [weather]
    python -m deckplate probe            numbered tiles, then listen 30s
    python -m deckplate listen 60        print key events for a minute
    python -m deckplate show 0 4 pic.png put a picture on the top right key
    python -m deckplate brightness 60
    python -m deckplate clear
    python -m deckplate calibrate-strip  rulers on the strip panels
    python -m deckplate calibrate-grid   one picture across the keys, to measure the gaps
    python -m deckplate layout           print the key grid
    python -m deckplate devices          list known decks that are plugged in
</summary>
<remarks>
This module is the only thing in the package that prints to a terminal and
reads exit codes: 0 for success, 1 for "nothing found" or "no deck", 2 for a
bad argument or an unreadable config. Anything that has to be said to a
person is said here, so the modules underneath can raise and stay quiet.

Every command that touches hardware goes through <see cref="_open"/> and then
through <see cref="device.Deck"/>, which means the safety gates apply to the
command line exactly as they do to the daemon. Nothing here passes
``experimental`` or ``allow_experimental``, and nothing here may ever be
changed to. The calibrate commands look like test tools but are not: they
send ordinary key images through the proven commands, the same way tiles are
sent in normal running.

Only one thing may hold the deck at a time. A command that opens the deck
while the daemon is running will fight it for the USB device, so the
calibrate commands say in their own text that the daemon must be stopped
first. The exception is ``gui``, which needs the daemon running because it
only shows the page the daemon serves.

Heavy and optional imports live inside the command functions rather than at
module scope, so that ``--help`` and the commands that need no hardware stay
fast and keep working on a machine with no HID library installed.

This docstring is also the ``--help`` description, so the command list above
is user facing text and is kept in step with <see cref="build_parser"/>.
</remarks>
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime

from . import __version__, images, layout, protocol
from .config import ConfigError, default_config_path, ensure_config, load
from .device import Deck, DeviceNotFound, enumerate_known

RECONNECT_SECONDS = 3


def _stamp() -> str:
    """
    <summary>
    The wall clock as HH:MM:SS.mmm, for log lines.
    </summary>
    <returns>A string.</returns>
    """
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def say(text: str) -> None:
    """<summary>
    Print one timestamped progress line to standard output.
    </summary>
    <param name="text">The message, with no timestamp and no trailing newline.</param>
    <remarks>
    Flushed on every call. Progress lines are read while a command is still
    running, and under systemd or a pipe the default block buffering would
    hold them back until the command ended, which is exactly when they stop
    being any use.

    This is passed down as the ``log`` callback to the controller and the API
    server, so their output interleaves with the command line's own on the
    same clock. The millisecond stamp is there because key events and tile
    pushes are the things being timed.
    </remarks>
    """
    print(f"{_stamp()} {text}", flush=True)


def cmd_devices(_: argparse.Namespace) -> int:
    """<summary>
    List every known deck currently plugged in, with the interface path.
    </summary>
    <param name="_">Parsed arguments, unused: this command takes none.</param>
    <returns>0 when at least one deck was found, 1 when none was.</returns>
    <remarks>
    This is the first thing to run when the deck seems absent, because it
    only enumerates and never opens. On Linux a deck that appears here but
    cannot be opened by the other commands is a missing udev rule rather than
    a hardware fault: the usb id printed is the one the rules file matches.
    </remarks>
    """
    found = enumerate_known()
    if not found:
        print("no known deck is plugged in")
        return 1
    for spec, path in found:
        print(f"{spec.usb_id}  {spec.name}  protocol v{spec.protocol_version}  "
              f"{spec.image_size}px  {path.decode(errors='replace')}")
    return 0


def cmd_layout(_: argparse.Namespace) -> int:
    """<summary>
    Print the key grid as the deck numbers it, keys then display strip.
    </summary>
    <param name="_">Parsed arguments, unused: this command takes none.</param>
    <returns>Always 0. No deck is opened, so there is nothing to fail.</returns>
    <remarks>
    Worth printing before using ``show`` or ``probe``, because the deck's own
    key numbers do not run left to right: they count down the columns, so the
    top row reads 13 10 7 4 1 and then the strip. Every other command in this
    module takes row and column and converts, so this is the one place the
    raw numbering is visible.
    </remarks>
    """
    for row in layout.grid():
        lcd = "  ".join(f"{key:2d}" for key in row[:layout.LCD_COLUMNS])
        print(f"{lcd}  |  {row[layout.STRIP_COLUMN]:2d}")
    print("left five columns are keys, right column is the display strip")
    return 0


def _open() -> Deck:
    """<summary>
    Open the one plugged in deck and bring it up ready for images.
    </summary>
    <returns>An open Deck, already initialised. The caller owns it and must
    close it, which the ``with`` blocks here do.</returns>
    <remarks>
    Every hardware command in this module goes through here rather than
    calling ``Deck.open`` itself, so there is exactly one place where the
    command line touches a deck and one place a gate would have to be
    defeated. It opens without ``allow_experimental`` and must stay that way.

    ``initialise`` sends the proven wake and connect sequence. Skipping it
    leaves the deck accepting writes and showing nothing, which reads as
    broken hardware rather than a missed call.
    </remarks>
    
    <exception cref="DeviceNotFound">No known deck is plugged in, or it could
    not be opened. On Linux that usually means the udev rule.</exception>"""
    deck = Deck.open()
    say(f"opened {deck.spec.name} ({deck.spec.usb_id})")
    deck.initialise()
    return deck


def cmd_listen(args: argparse.Namespace) -> int:
    """<summary>
    Open the deck and print key events for a while, changing nothing on it.
    </summary>
    <param name="args">Parsed arguments; ``seconds`` is how long to listen.</param>
    <returns>Always 0. Hearing no events is a result, not an error.</returns>
    <remarks>
    The tiles already on the deck are left exactly as they are: this command
    sends nothing at all beyond the open and initialise. It is the safe way
    to check that presses are getting through when the daemon seems deaf, but
    the daemon must be stopped first or the two will fight over the device.
    </remarks>
    <see cref="_listen"/>
    """
    with _open() as deck:
        _listen(deck, args.seconds)
    return 0


def _listen(deck: Deck, seconds: float) -> None:
    """<summary>
    Print every key press and release for a fixed span, then a count.
    </summary>
    <param name="deck">An open, initialised deck. Not closed here.</param>
    <param name="seconds">How long to listen for, from now.</param>
    <remarks>
    The 250ms read timeout is what makes the deadline real: a timeout is the
    normal answer when nobody is pressing anything, so it is skipped
    silently, and the loop gets to check the clock four times a second rather
    than blocking until the next press.

    Rows and columns are printed beside the deck's own key number because the
    two do not agree, and the config file is written in rows and columns.
    </remarks>
    """
    say(f"listening for {seconds:.0f}s, press keys on the deck")
    deadline = time.monotonic() + seconds
    count = 0
    while time.monotonic() < deadline:
        event = deck.read_event(250)
        if event is None:
            continue
        count += 1
        row, column = layout.position(event.key)
        state = "press" if event.pressed else "release"
        say(f"key {event.key:2d} {state:7s} row {row} column {column}")
    say(f"done, {count} events")


def cmd_probe(args: argparse.Namespace) -> int:
    """<summary>
    Put a numbered tile on every key, then listen, to prove the deck works.
    </summary>
    <param name="args">Parsed arguments; ``brightness`` and ``seconds``.</param>
    <returns>Always 0.</returns>
    <remarks>
    The one command that answers "is anything working at all": it exercises
    brightness, image push, commit and key reading in that order, so whatever
    is broken shows up at the step it belongs to.

    The expected top row is printed so the numbering can be checked by eye
    against what the deck shows. If the tiles land in a different order the
    fault is in the key numbering, not in the images.

    This overwrites whatever the daemon had drawn. Restart the daemon to get
    the normal tiles back.
    </remarks>
    """
    with _open() as deck:
        deck.set_brightness(args.brightness)
        say(f"pushing {protocol.KEY_COUNT} numbered tiles at {deck.image_size}px")
        for key in range(1, protocol.KEY_COUNT + 1):
            deck.set_key_image(key, images.numbered_tile(key, deck.image_size))
        deck.commit()
        say("tiles committed; expected top row left to right: 13 10 7 4 1 16")
        if args.seconds > 0:
            _listen(deck, args.seconds)
    return 0


def cmd_calibrate_strip(args: argparse.Namespace) -> int:
    """<summary>
    Frames on the strip panels, each a set distance in from the edge, to
    measure the visible area.
    </summary>
    <param name="args">Parsed arguments; ``insets`` is three numbers,
    smallest first, one per strip panel.</param>
    <returns>0 once the frames are shown, 2 when the insets are unusable.</returns>
    <remarks>
    The strip panels crop what is sent to them and the amount is not
    published anywhere, so it is measured by eye: a frame drawn N pixels in
    from every edge that is fully visible proves the panel shows at least 95
    minus 2N pixels across. Several runs with different numbers narrow it
    down.

    A frame is also put on one ordinary key as a control. That one is never
    cropped, so a frame missing there means the drawing is wrong rather than
    the panel.

    Only proven commands are used: these are ordinary key images, sent the
    same way the daemon sends tiles. The daemon must be stopped first, and
    restarting it puts the normal tiles back.
    </remarks>
    """
    from . import tiles

    try:
        insets = [int(x) for x in args.insets.split(",")]
        if len(insets) != layout.ROWS or any(not 0 <= i < 40 for i in insets):
            raise ValueError
    except ValueError:
        print("error: --insets needs three numbers from 0 to 39, for example 4,8,12", file=sys.stderr)
        return 2
    with _open() as deck:
        size = deck.image_size
        for row, inset in enumerate(insets):
            deck.set_position_image(row, layout.STRIP_COLUMN, tiles.frame_tile(size, inset, f"S{row + 1}"))
        deck.set_position_image(0, layout.LCD_COLUMNS - 1, tiles.frame_tile(size, insets[0], "KEY"))
        deck.commit()
    say(f"frames shown on the strip panels at {insets[0]}, {insets[1]} and {insets[2]} px in from the edge")
    print()
    print("Each strip panel shows a yellow frame drawn that many pixels in from every edge,")
    print("with the number in the middle. For each panel say whether the whole frame is")
    print("visible, and if not, which sides are missing. A complete frame at N means the")
    print("panel shows at least 95 minus 2N pixels across. Try again with other numbers,")
    print("for example --insets 6,7,9, to narrow it down. The KEY tile always shows its")
    print("frame in full. Restart the daemon to get the normal tiles back.")
    return 0


def parse_pitch(text: str) -> tuple[int, int]:
    """<summary>
    Read a pitch given as two numbers, across then down, for example 142,160.
    </summary>
    <param name="text">The raw ``--pitch`` value. An "x" between the numbers
    is accepted as well as a comma, because 142x160 is how anyone used to
    image sizes will type it.</param>
    <returns>The pitch as (across, down) in key pixels.</returns>
    <remarks>
    A pitch is centre to centre distance measured in the key's own 95 pixel
    units, not in millimetres and not in screen pixels, so the numbers are
    larger than the key is wide: the gap between keys is included. The range
    check comes from the config module so the command line and the config
    file refuse the same values.
    </remarks>
    <exception cref="ValueError">When it is not two whole numbers in range.</exception>
    """
    from .config import KEY_PITCH_RANGE
    parts = [part.strip() for part in text.replace("x", ",").split(",")]
    if len(parts) != 2:
        raise ValueError("pitch needs two numbers, across then down, for example 142,160")
    low, high = KEY_PITCH_RANGE
    values = []
    for part in parts:
        if not part.isdigit() or not low <= int(part) <= high:
            raise ValueError(f"pitch values must be whole numbers from {low} to {high}")
        values.append(int(part))
    return values[0], values[1]


def cmd_calibrate_grid(args: argparse.Namespace) -> int:
    """<summary>
    One picture cut across the keys at a guessed pitch, to find the real one.
    </summary>
    <param name="args">Parsed arguments; ``pitch`` overrides the config, and
    ``config`` picks a config file other than the per user one.</param>
    <returns>0 once the pattern is shown, 2 when no usable pitch was given or
    found.</returns>
    <remarks>
    The daemon must not be running: both would hold the deck. Only the proven
    commands are used, one image per key, exactly as the daemon sends tiles.

    The read is by eye and the direction of the error is the useful part. A
    line that steps inwards at every gap means the pitch is too small,
    outwards means too large, and the two axes are judged separately because
    the keys are not square on the deck.

    With no ``--pitch`` the config is read, and a config with no pitch yet is
    a stop rather than a guess: a made up starting value would be read as a
    measurement by whoever ran it.
    </remarks>
    """
    from . import tiles

    if args.pitch:
        try:
            pitch = parse_pitch(args.pitch)
        except ValueError as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
    else:
        try:
            pitch = load(ensure_config(args.config)).deck.key_pitch
        except ConfigError as err:
            print(f"error: config could not be read ({err})", file=sys.stderr)
            return 2
        if pitch is None:
            print("error: no pitch known yet. Measure the deck with a ruler, the distance between key", file=sys.stderr)
            print("centres divided by the visible width of a key, times 95, then pass --pitch 142,160", file=sys.stderr)
            return 2
    with _open() as deck:
        size = deck.image_size
        for (row, column), tile in tiles.grid_pattern(size, *pitch).items():
            deck.set_position_image(row, column, tile)
        deck.commit()
    say(f"grid pattern shown at a pitch of {pitch[0]} across and {pitch[1]} down")
    print()
    print("Look at the deck. Two yellow diagonals, a blue circle and two grey centre lines")
    print("are drawn across all fifteen keys as if they were one picture. If the pitch is")
    print("right, every line runs straight through the gaps between keys. If a line steps")
    print("inwards at each gap the pitch is too small; outwards and it is too large. Try")
    print("again with other numbers, across and down separately, until they run true, then")
    print("put the numbers in the config:")
    print()
    print(f"    [deck]")
    print(f"    key_pitch_x = {pitch[0]}")
    print(f"    key_pitch_y = {pitch[1]}")
    print()
    print("Restart the daemon to get the normal tiles back.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """<summary>
    Put one picture file on one key, by row and column.
    </summary>
    <param name="args">Parsed arguments; ``row``, ``column`` and ``image``.</param>
    <returns>Always 0. A bad row or column raises out of the layout module
    and a bad picture out of the images module, both reported by
    <see cref="main"/>.</returns>
    <remarks>
    The picture is scaled and re-encoded to the deck's own image size before
    it is sent; the file on disk can be any size and any supported format.
    Nothing appears until ``commit``, which is why it is sent inside the same
    block rather than left to the caller.

    One key only, and no strip panel handling here beyond whatever the layout
    module allows. This is a spot check, not a way to dress the deck: the
    daemon overwrites it at the next redraw.
    </remarks>
    """
    with _open() as deck:
        jpeg = images.from_file(args.image, deck.image_size)
        deck.set_key_jpeg(layout.key_number(args.row, args.column), jpeg)
        deck.commit()
        say(f"{args.image} shown at row {args.row} column {args.column}")
    return 0


def cmd_brightness(args: argparse.Namespace) -> int:
    """<summary>
    Set the deck's backlight, 0 to 100.
    </summary>
    <param name="args">Parsed arguments; ``percent``.</param>
    <returns>Always 0.</returns>
    <remarks>
    The setting does not survive an unplug, and the daemon sets its own from
    the config at startup, so this is a temporary change for looking at the
    deck rather than a way to configure it. 0 is dark but not asleep: the
    deck is still listening and still holding its images.
    </remarks>
    """
    with _open() as deck:
        deck.set_brightness(args.percent)
        say(f"brightness {args.percent}")
    return 0


def cmd_clear(_: argparse.Namespace) -> int:
    """<summary>
    Blank every key and commit, leaving the deck lit but empty.
    </summary>
    <param name="_">Parsed arguments, unused: this command takes none.</param>
    <returns>Always 0.</returns>
    <remarks>
    Clearing only changes what is displayed. Nothing on the deck is stored,
    reset or erased, and nothing in the config is touched, so restarting the
    daemon puts every tile straight back. Useful for leaving the deck tidy
    after a probe or a calibration run.
    </remarks>
    """
    with _open() as deck:
        deck.clear()
        deck.commit()
        say("all keys cleared")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """<summary>
    Open the configuration page in a window, on the running daemon's port.
    </summary>
    <param name="args">Parsed arguments; ``config`` picks the config file and
    ``browser`` asks for the normal browser instead of a window.</param>
    <returns>0 once the page has been shown, 1 when no daemon is listening.</returns>
    <remarks>
    The only command here that needs the daemon running rather than stopped:
    it opens no deck of its own and shows a page the daemon serves.

    An unreadable config is not fatal. The port is the only thing needed, so
    a warning is printed and the default 8765 is assumed, which is right far
    more often than giving up would be helpful.

    A non loopback ``bind`` in the config is deliberately not used as the
    host. The page is always opened on 127.0.0.1, because the window runs on
    this machine and the local address is the one that works regardless of
    what else the daemon is listening on.
    </remarks>
    """
    from . import gui

    path = ensure_config(args.config)
    host, port = "127.0.0.1", 8765
    try:
        settings = load(path).server
        port = settings.port
        if settings.bind in ("127.0.0.1", "localhost", "::1"):
            host = settings.bind
    except ConfigError as err:
        say(f"config could not be read ({err}); assuming port 8765")
    return gui.run(host, port, log=say, browser=args.browser)


def cmd_config_path(args: argparse.Namespace) -> int:
    """<summary>
    Print where the config file lives, creating nothing.
    </summary>
    <param name="args">Parsed arguments; ``config`` echoes back an explicit
    path instead of working out the default.</param>
    <returns>Always 0.</returns>
    <remarks>
    The path is printed whether or not a file is there, so this answers
    "where should it go" as well as "where is it". Nothing but the path goes
    to standard output, which makes it safe to use inside another command.
    Use ``init-config`` to actually create one.
    </remarks>
    """
    print(args.config or default_config_path())
    return 0


def cmd_init_config(args: argparse.Namespace) -> int:
    """<summary>
    Write the default config file if there is not one already.
    </summary>
    <param name="args">Parsed arguments; ``config`` writes somewhere other
    than the per user path.</param>
    <returns>Always 0, whether a file was written or one was already there.</returns>
    <remarks>
    Safe to run repeatedly: an existing config is never overwritten, never
    merged and never upgraded, so nothing already edited can be lost. The
    path is printed either way, which is the answer wanted in both cases.
    </remarks>
    """
    path = ensure_config(args.config)
    print(f"config at {path}")
    return 0


def cmd_geocode(args: argparse.Namespace) -> int:
    """<summary>
    Look up a place name and print coordinates ready to paste into [weather].
    </summary>
    <param name="args">Parsed arguments; ``name`` is the place to look up.</param>
    <returns>0 when at least one place matched, 1 when none did.</returns>
    <remarks>
    This is the only command that needs the network, and it opens no deck.
    Several matches are normal and all of them are printed: place names
    repeat across countries, so the region and country are shown to tell them
    apart and the choice is left to the reader rather than guessed at.

    The latitude and longitude lines are printed in config file spelling so
    they can go straight into the weather section unedited.
    </remarks>
    """
    from .weather import geocode

    places = geocode(args.name)
    if not places:
        print(f"nothing found for '{args.name}'")
        return 1
    for place in places:
        where = ", ".join(part for part in (place.name, place.region, place.country) if part)
        print(f"{where}\n    latitude = {place.latitude}\n    longitude = {place.longitude}")
    return 0


def resolve_conflicts(policy: str, interactive: bool, ask=input, log=say) -> None:
    """<summary>
    Stop, keep or ask about software that would fight over the deck.
    </summary>
    <param name="policy">"ask", "stop" or "keep". Anything else behaves like
    "stop", because an unknown policy must not silently mean "carry on".</param>
    <param name="interactive">True only when there is a real terminal to ask
    at. Under systemd there is not.</param>
    <param name="ask">The prompt function, replaced in the tests.</param>
    <param name="log">Where notices go, replaced in the tests.</param>
    <remarks>
    Only one process can hold the deck. If the official software is running
    the two take turns writing to it and the display flickers between two
    sets of tiles, which looks like a fault in this daemon rather than a
    clash, so it is worth saying out loud even when nothing is stopped.

    When asking is not possible the other software is left alone and a
    warning says so. Killing a process nobody agreed to kill, on a machine
    with nobody watching, is the worse of the two outcomes. The warning names
    the flag and the config key that settle it without a prompt.

    Failing to stop a process is reported and not raised: it is usually a
    permissions matter, and the daemon can still start and fight for the
    deck, which is better than not starting.
    </remarks>
    """
    from . import conflicts

    found = conflicts.find_conflicts()
    if not found:
        return
    names = conflicts.describe(found)
    if policy == "keep":
        log(f"note: {names} is also running and will fight over the deck")
        return
    if policy == "ask":
        if not interactive:
            log(f"warning: {names} is also running; cannot ask without a terminal, "
                f"carrying on. Use --stop-official or set official_software in the config")
            return
        answer = ask(f"{names} is running and will fight over the deck. Stop it? [Y/n] ")
        if answer.strip().lower() in ("n", "no"):
            log("leaving it running")
            return
    for process in found:
        if conflicts.stop(process):
            log(f"stopped {process.name} (pid {process.pid})")
        else:
            log(f"could not stop {process.name} (pid {process.pid})")


def cmd_run(args: argparse.Namespace) -> int:
    """<summary>
    The daemon: drive the deck from the config file until stopped.
    </summary>
    <param name="args">Parsed arguments; ``config``, ``seconds`` for a bounded
    test run, and the two official software flags.</param>
    <returns>0 on a clean stop or a finished timed run, 2 when the config
    could not be read.</returns>
    <remarks>
    The order matters and is not obvious. The config is read and validated
    before anything else, so a typo fails at once rather than after the deck
    has been taken over. Conflicting software is settled next, while there is
    still a terminal to ask at. The API server is started third and, crucially,
    outside the deck loop: it must stay up across an unplug so the page can
    say what is wrong instead of going dead.

    Then the deck loop runs forever. A missing or lost deck is not a failure:
    it waits and tries again every few seconds, which is what a cable pulled
    out and put back looks like. The controller is published through the
    holder while it is alive and cleared in the ``finally``, so the API always
    knows whether there is a deck without reaching for one.

    The reloaded config is carried out of the controller on the way round the
    loop. Edits made through the page would otherwise be lost at the next
    replug, which is the trap here: the local variable is the live config
    from that point on, not the one read from disk at startup.

    ``--seconds`` exists for tests and for a supervised first run. The
    remaining time is recomputed each time round so a reconnect cannot extend
    the run past its deadline.
    </remarks>
    """
    from .controller import Controller

    path = ensure_config(args.config)
    try:
        config = load(path)
    except ConfigError as err:
        print(f"error in {path}: {err}", file=sys.stderr)
        return 2
    say(f"config {path}")

    policy = config.deck.official_software
    if args.stop_official:
        policy = "stop"
    elif args.keep_official:
        policy = "keep"
    resolve_conflicts(policy, interactive=sys.stdin is not None and sys.stdin.isatty())

    from . import server as api

    holder = api.ControllerHolder()
    api_server = None
    if config.server.enabled:
        try:
            api_server = api.start(config.server, holder, log=say)
            say(f"api at http://{config.server.bind}:{api_server.port}/")
        except OSError as err:
            say(f"api not started: {err}")

    deadline = None if args.seconds is None else time.monotonic() + args.seconds
    try:
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining is not None and remaining <= 0:
                return 0
            try:
                deck = Deck.open()
            except DeviceNotFound as err:
                say(f"{err}; retrying in {RECONNECT_SECONDS}s")
                time.sleep(RECONNECT_SECONDS)
                continue
            say(f"opened {deck.spec.name} ({deck.spec.usb_id})")
            if deck.is_fake:
                say("NO HARDWARE: driving a fake deck in memory because device.FAKE_DECK_WHEN_ABSENT is on")
            controller = Controller(deck, config, log=say)
            holder.controller = controller
            try:
                controller.start()
                controller.run(seconds=remaining)
                if remaining is not None:
                    return 0
            except (OSError, DeviceNotFound) as err:
                say(f"deck lost: {err}; reconnecting in {RECONNECT_SECONDS}s")
                time.sleep(RECONNECT_SECONDS)
            finally:
                holder.controller = None
                config = controller.config  # keep a reloaded config across reconnects
                deck.close()
    except KeyboardInterrupt:
        say("stopping")
        return 0
    finally:
        if api_server is not None:
            api_server.stop()


def help_text(doc: str | None) -> str:
    """<summary>
    Strip the XML doc tags out of a module docstring so it can be shown as
    command line help.
    </summary>
    <param name="doc">A module docstring in the house comment style, or None.</param>
    <returns>The prose alone, with the tag lines removed.</returns>
    <remarks>
    Every file in this project carries a tagged header block, and argparse
    prints whatever description it is handed verbatim. Passing the raw
    docstring therefore shows tags to the user, which is what this exists to
    prevent. Only lines that are nothing but a tag are dropped, so prose and
    the indented examples inside the block survive untouched. A cross
    reference written inline in a sentence is unwrapped to the bare name
    rather than dropped, because deleting the line would take the sentence
    around it with it.
    </remarks>
    """
    if not doc:
        return ""
    tag = re.compile(r"^\s*</?(?:summary|remarks|param|returns|exception|see|seealso)\b[^>]*>\s*$")
    ref = re.compile(r"<(?:see|seealso)\s+cref=\"([^\"]+)\"\s*/?>")
    kept = (ref.sub(r"\1", line) for line in doc.splitlines() if not tag.match(line))
    return "\n".join(kept).strip("\n")


def build_parser() -> argparse.ArgumentParser:
    """<summary>
    Build the whole argument parser, one subcommand per ``cmd_`` function.
    </summary>
    <returns>The parser, ready to parse. Nothing is executed by building it.</returns>
    <remarks>
    Every subparser sets ``func`` to its command function, which is the only
    dispatch there is: <see cref="main"/> calls whatever ``func`` the parse
    left behind. A new command therefore needs its parser here, its function
    above, and its line in the module docstring, which is the ``--help``
    description.

    A subcommand is required, so bare ``deckplate`` prints usage and exits 2
    rather than doing something. The mutually exclusive group on ``run``
    makes the two official software flags refuse to be given together, since
    "stop it" and "keep it" have no sensible combined meaning.

    The name ``p`` is reused all the way down on purpose: each block is
    finished by its ``set_defaults`` before the next begins.
    </remarks>
    """
    parser = argparse.ArgumentParser(prog="deckplate", description=help_text(__doc__),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="drive the deck from the config file")
    p.add_argument("--config", help="config file path (default: the per user one)")
    p.add_argument("--seconds", type=float, default=None, help="stop after this long (testing)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--stop-official", action="store_true",
                       help="stop the official deck software if it is running, without asking")
    group.add_argument("--keep-official", action="store_true",
                       help="leave the official deck software running, without asking")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("gui", help="open the configuration page in a window (the daemon must be running)")
    p.add_argument("--config")
    p.add_argument("--browser", action="store_true", help="open in the normal browser instead of a window")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("init-config", help="write the default config if it does not exist")
    p.add_argument("--config")
    p.set_defaults(func=cmd_init_config)

    p = sub.add_parser("config-path", help="print the config file path")
    p.add_argument("--config")
    p.set_defaults(func=cmd_config_path)

    p = sub.add_parser("geocode", help="look up coordinates for the weather tile")
    p.add_argument("name")
    p.set_defaults(func=cmd_geocode)

    sub.add_parser("devices", help="list known decks that are plugged in").set_defaults(func=cmd_devices)
    sub.add_parser("layout", help="print the key grid").set_defaults(func=cmd_layout)

    p = sub.add_parser("probe", help="numbered tiles, then listen for key events")
    p.add_argument("--seconds", type=float, default=30)
    p.add_argument("--brightness", type=int, default=80)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("listen", help="print key events")
    p.add_argument("seconds", type=float, nargs="?", default=30)
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("calibrate-strip", help="show measuring frames on the strip panels to find their visible area")
    p.add_argument("--insets", default="4,8,12", help="three distances in pixels from the edge, one per panel")
    p.set_defaults(func=cmd_calibrate_strip)

    p = sub.add_parser("calibrate-grid", help="one picture cut across every key at a guessed pitch, to measure the gaps")
    p.add_argument("--pitch", help="centre to centre distance across,down in key pixels, for example 142,160; default from the config")
    p.add_argument("--config")
    p.set_defaults(func=cmd_calibrate_grid)

    p = sub.add_parser("show", help="put a picture on one key")
    p.add_argument("row", type=int)
    p.add_argument("column", type=int)
    p.add_argument("image")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("brightness", help="set brightness 0 to 100")
    p.add_argument("percent", type=int)
    p.set_defaults(func=cmd_brightness)

    sub.add_parser("clear", help="blank every key").set_defaults(func=cmd_clear)
    return parser


def main(argv: list[str] | None = None) -> int:
    """<summary>
    Parse the arguments and run the chosen command.
    </summary>
    <param name="argv">Arguments without the program name. None means take
    them from the real command line, which is what the entry point does; the
    tests pass a list.</param>
    <returns>The command's exit code. 2 comes out of argparse itself for a
    bad argument, and 1 for a deck that is not there.</returns>
    <remarks>
    DeviceNotFound is caught here and turned into one plain line on standard
    error, because a traceback for an unplugged deck tells the reader nothing
    they did not already know. Every other exception is deliberately left to
    surface with its traceback: those are faults worth seeing in full.

    Returns a code rather than exiting, so the tests can call it directly.
    The module level guard below is the only place that exits.
    </remarks>
    """
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DeviceNotFound as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
