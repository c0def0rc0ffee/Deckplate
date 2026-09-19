"""<summary>
The controller: the loop that owns the deck, turns presses into actions,
redraws what has changed, and answers the configuration page.
</summary>
<remarks>
This is the widest file in the suite because the controller is where
everything meets. It is driven entirely through fakes: a deck that records
calls instead of writing USB packets, and a clock the test moves by hand so
that sleeping, blinking, animation and press timing can be checked in
microseconds rather than waited out.

Two themes run through the whole file. The first is that only the seven
proven commands may ever reach a deck, which is asserted directly in
<see cref="test_only_proven_commands_reach_the_deck"/> and guarded again in
the preview tests, since the configuration page asks for pictures larger
than the hardware could accept. The second is redrawing as little as
possible: every tile sent is a JPEG over the same USB link the key presses
come back on, so several tests assert an exact list of redrawn positions
rather than merely that the right one was among them.

Key numbers in the events are the firmware's own numbering, which runs down
each column from the right, so the comments beside them naming a row and
column are load bearing. Column 5 is the strip.
</remarks>
"""

from datetime import datetime, timedelta

import pytest

from deckplate import config as cfg
from deckplate import focus
from deckplate import controller as controller_module
from deckplate import layout
from deckplate import tiles as tiles_module
from deckplate.actions import ActionRunner
from deckplate.controller import Controller
from deckplate.layout import STRIP_COLUMN
from deckplate.protocol import KeyEvent
from deckplate.weather import Observation, WeatherService

# The everyday configuration for most of this file: two pages, a key that
# opens a URL, a key that pages forward, and a key that dims. The one minute
# sleep and the switched off press flash are what let the idle and redraw
# tests run without either getting in the way.
TEXT = """
[deck]
brightness = 70
sleep_after_minutes = 1
press_flash = false

[[pages]]
name = "Main"

[[pages.keys]]
row = 0
column = 0
label = "Web"
action = { type = "url", url = "https://example.org" }

[[pages.keys]]
row = 2
column = 4
label = "Next"
action = { type = "page", page = "next" }

[[pages]]
name = "Second"

[[pages.keys]]
row = 0
column = 0
label = "Dim"
action = { type = "brightness", delta = -20 }
"""


class FakeSpec:
    """
    <summary>
    A deck spec with the real Soomfon id and screen size, and nothing else.
    </summary>
    """
    name = "Fake deck"
    usb_id = "1500:3003"
    screen = (854, 480)


class FakeDeck:
    """<summary>
    A deck that records what it was asked to do and never sends anything.
    </summary>
    <remarks>
    The call log is the whole point: order matters as much as content, so
    tests index into it to prove that a clear happened before a tile, or
    that a commit followed one. Keep appending in the same shapes, since
    every assertion in this file matches on those tuples.

    Tiles and frames are recorded by position only, with the picture
    discarded. Anything that needs the picture itself replaces the method,
    as the render size tests do. ``reset`` is used constantly to make an
    assertion about one tick rather than about the whole session.
    </remarks>
    """

    image_size = 95
    spec = FakeSpec()

    def __init__(self):
        """
        <summary>
        Start with empty call and event logs.
        </summary>
        """
        self.calls = []
        self.events = []

    def initialise(self):
        """
        <summary>
        Record the call.
        </summary>
        """
        self.calls.append(("init",))

    def set_brightness(self, p):
        """
        <summary>
        Record the call.
        </summary>
        <param name="p">Percent.</param>
        """
        self.calls.append(("brightness", p))

    def clear(self, key=None):
        """
        <summary>
        Record the call.
        </summary>
        <param name="key">One key, or None for all.</param>
        """
        self.calls.append(("clear", key))

    def set_position_image(self, row, column, image):
        """
        <summary>
        Record the position; the image itself is dropped.
        </summary>
        <param name="row">Row.</param>
        <param name="column">Column.</param>
        <param name="image">Ignored.</param>
        """
        self.calls.append(("tile", row, column))

    def set_position_jpeg(self, row, column, jpeg):
        """
        <summary>
        Record the position; the bytes are dropped.
        </summary>
        <param name="row">Row.</param>
        <param name="column">Column.</param>
        <param name="jpeg">Ignored.</param>
        """
        self.calls.append(("frame", row, column))

    def commit(self):
        """
        <summary>
        Record the call.
        </summary>
        """
        self.calls.append(("commit",))

    def sleep(self):
        """
        <summary>
        Record the call.
        </summary>
        """
        self.calls.append(("sleep",))

    def keepalive(self):
        """
        <summary>
        Record the call.
        </summary>
        """
        self.calls.append(("keepalive",))

    def read_event(self, timeout_ms):
        """<summary>The next scripted key event, or None as a real timeout would give.</summary>"""
        return self.events.pop(0) if self.events else None

    def tiles(self):
        """<summary>Just the tile redraws from the call log, in order.</summary>"""
        return [c for c in self.calls if c[0] == "tile"]

    def reset(self):
        """<summary>Empty the call log so the next assertion covers one step only.</summary>"""
        self.calls.clear()


class FakeTime:
    """<summary>
    A clock the test moves by hand, keeping the monotonic and wall clock
    readings in step.
    </summary>
    <remarks>
    Both are needed and they do different jobs. The monotonic value drives
    idle sleeping, keepalives, animation and press timing; the wall clock
    drives the clock panel, which is why the start time sits a few minutes
    past the hour, with a minute boundary close enough to cross in one
    advance. Advancing moves both, so a test cannot accidentally hold one
    still.
    </remarks>
    """

    def __init__(self):
        """
        <summary>
        Start at a fixed monotonic time and a fixed wall clock.
        </summary>
        """
        self.mono = 100.0
        self.wall = datetime(2026, 9, 10, 14, 5, 0)

    def advance(self, seconds):
        """<summary>Move both clocks forward by the same amount.</summary>"""
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


@pytest.fixture
def setup(tmp_path):
    """<summary>
    A controller wired to a fake deck, a hand driven clock and a recording
    URL opener, with a real configuration file on disk behind it.
    </summary>
    <returns>The controller, deck, clock, opened URL list and config path.</returns>
    <remarks>
    The file really is written to disk because the controller watches it for
    changes, and the reload test needs a path whose timestamp can be moved.
    The weather service is given a fetcher that answers instantly from
    memory: nothing here goes near the network.

    The runner is replaced after construction rather than passed in, which
    is what keeps key presses from opening real browser windows or launching
    real programs while the suite runs.
    </remarks>
    """
    path = tmp_path / "config.toml"
    path.write_text(TEXT, encoding="utf-8")
    config = cfg.load(path)
    deck = FakeDeck()
    clock = FakeTime()
    urls = []
    weather = WeatherService(config.weather, fetcher=lambda s: Observation(10, 0, "Clear", "sun", "C"),
                             monotonic=lambda: clock.mono)
    controller = Controller(deck, config, now=lambda: clock.wall, monotonic=lambda: clock.mono,
                            weather=weather, log=lambda t: None)
    controller.runner = ActionRunner(controller, open_url=urls.append, log=lambda t: None)
    return controller, deck, clock, urls, path


def test_start_renders_every_panel_once(setup):
    """<summary>
    Pins the opening sequence: initialise, set the brightness, draw all
    eighteen panels, then commit.
    </summary>
    <remarks>
    Eighteen is fifteen keys plus the three strip panels, and every one must
    be drawn even where the configuration says nothing about it, or the deck
    would come up showing whatever the previous owner of the device left on
    those screens. Column five is the strip, so its presence proves the
    strip is part of the same pass rather than an afterthought.

    The commit last is what makes the whole page appear at once instead of
    filling in panel by panel, and it is asserted as the final call rather
    than as merely present.
    </remarks>
    """
    controller, deck, *_ = setup
    controller.start()
    assert deck.calls[0] == ("init",)
    assert ("brightness", 70) in deck.calls
    tiles = deck.tiles()
    assert len(tiles) == 18
    assert ("tile", 0, 5) in tiles and ("tile", 2, 4) in tiles
    assert deck.calls[-1] == ("commit",)


def test_start_blanks_the_whole_screen_before_drawing_tiles(setup):
    """<summary>
    The surround goes dark by a clear all on connect, as the official app
    does.

    The clear (of every key, 0xFF) and its commit must come before the first
    tile is drawn, or the redraw would land under the blank.
    </summary>
    <remarks>
    The keys are regions of one larger screen, and the area around them is
    whatever was last left there. Clearing every key is how that surround is
    darkened, and it is done with CLE, one of the seven proven commands.
    There is deliberately no whole screen picture command in use: the deck
    does have one, and it is half of what permanently disabled a deck.

    The ordering is the entire test. A clear issued after the tiles were
    queued would wipe the freshly drawn page and leave the deck blank until
    something else happened to redraw it.
    </remarks>
    """
    controller, deck, *_ = setup
    controller.start()
    clear_at = deck.calls.index(("clear", None))
    first_tile_at = next(i for i, c in enumerate(deck.calls) if c[0] == "tile")
    assert clear_at < first_tile_at
    # A commit falls between the clear and the first tile, so the blank is
    # pushed to the screen before the tiles are queued on top of it.
    assert any(deck.calls[i] == ("commit",) for i in range(clear_at, first_tile_at))


def test_press_runs_the_action_and_release_is_ignored(setup):
    """<summary>
    Pins a press as the thing that acts, and pins the three cases that must
    do nothing at all.
    </summary>
    <remarks>
    The release must not act on a key with no second actions, or every press
    would fire twice, which for a URL key means two browser tabs. A key with
    nothing configured must be silently ignored rather than logged or
    errored, since most decks have empty keys.

    The strip is the one worth spelling out: its panels report presses like
    any other key, but they are information displays and must never run an
    action, however the user has configured the page.
    </remarks>
    """
    controller, deck, clock, urls, _ = setup
    controller.start()
    controller.handle_event(KeyEvent(13, True))   # key 13 is row 0 column 0
    controller.handle_event(KeyEvent(13, False))
    assert urls == ["https://example.org"]
    controller.handle_event(KeyEvent(1, True))    # row 0 column 4: nothing configured
    controller.handle_event(KeyEvent(16, True))   # strip panel: never an action
    assert urls == ["https://example.org"]


def test_page_switch_rerenders_keys_only(setup):
    """<summary>
    Pins a page change redrawing the fifteen keys and leaving the strip
    alone, and pins the named and relative ways of moving between pages.
    </summary>
    <remarks>
    The strip shows the clock, the date and the weather, none of which
    belong to a page, so redrawing it would cost three more pictures over
    USB for no visible change at all. Asserting that no redraw names column
    five is what holds that.

    Moving by name and moving to the previous page are both pinned, since
    the previous page is remembered state rather than a step backwards
    through the list, and it is the only way back from a page reached by an
    action.
    </remarks>
    """
    controller, deck, clock, urls, _ = setup
    controller.start()
    deck.reset()
    controller.handle_event(KeyEvent(3, True))    # key 3 is row 2 column 4: "Next"
    assert controller.page.name == "Second"
    assert len(deck.tiles()) == 15
    assert all(c[2] != 5 for c in deck.tiles())
    controller.handle_event(KeyEvent(13, True))   # "Dim" on page two
    assert deck.calls[-1] == ("brightness", 50)
    controller.switch_page("Main")
    assert controller.page.name == "Main"
    controller.switch_page("previous")
    assert controller.page.name == "Second"


def test_clock_tile_is_resent_only_when_the_minute_changes(setup):
    """<summary>
    Pins the clock panel being redrawn on the minute boundary and at no
    other time.
    </summary>
    <remarks>
    The panel shows hours and minutes, so redrawing it on every tick would
    send a picture several times a second that is identical to the one
    already showing, stealing the link from the key presses. Twenty seconds
    passing must produce nothing; crossing into the next minute must produce
    exactly one redraw.

    The commit after the tile is checked by index rather than by presence:
    a commit issued before the tile was queued would leave the new time sat
    in the deck's buffer, so the clock would appear to be a minute slow
    until something else committed.
    </remarks>
    """
    controller, deck, clock, *_ = setup
    controller.start()
    deck.reset()
    clock.advance(20)
    controller.tick()
    assert deck.tiles() == []
    clock.advance(45)  # crosses 14:06
    controller.tick()
    assert deck.tiles() == [("tile", 0, 5)]
    assert deck.calls.index(("commit",)) > deck.calls.index(("tile", 0, 5))


def test_idle_sleep_and_wake_on_press(setup):
    """<summary>
    Pins the idle sleep at its configured minute, and pins the first press
    after it as a wake that runs no action.
    </summary>
    <remarks>
    The wake only press is the part that matters to anyone using the deck. A
    sleeping deck shows nothing, so the user cannot see what they are about
    to press; running the action on that blind press would fire whatever
    happened to be under their finger. The first press wakes and redraws,
    and only the second acts.

    Waking is a full reinitialise and a redraw of all eighteen panels,
    because the deck is not trusted to have kept its contents while blanked.
    </remarks>
    """
    controller, deck, clock, urls, _ = setup
    controller.start()
    clock.advance(30)
    controller.tick()
    assert not controller.asleep
    clock.advance(31)
    controller.tick()
    assert controller.asleep and ("sleep",) in deck.calls
    deck.reset()
    controller.handle_event(KeyEvent(13, True))   # first press only wakes, no action
    assert not controller.asleep
    assert urls == []
    assert deck.calls[0] == ("init",) and len(deck.tiles()) == 18
    controller.handle_event(KeyEvent(13, True))
    assert urls == ["https://example.org"]


def test_page_and_brightness_requests_wake_a_sleeping_deck(setup):
    """<summary>
    Pins a page change or a brightness change arriving from elsewhere as
    waking the deck rather than being applied invisibly.
    </summary>
    <remarks>
    These requests come from the configuration page and from focus
    following, not from a key press, so nothing else would wake the deck.
    Applying them while asleep would leave the user watching a black deck
    wondering why the page they just clicked had no effect, and the change
    would then appear later out of nowhere.
    </remarks>
    """
    controller, deck, clock, urls, _ = setup
    controller.start()
    controller.sleep_deck()
    assert controller.asleep
    controller.switch_page("Second")
    assert not controller.asleep and controller.page.name == "Second"
    controller.sleep_deck()
    controller.set_brightness(value=40)
    assert not controller.asleep and controller.brightness == 40


def test_a_wake_from_the_window_resets_the_idle_clock(setup):
    """<summary>
    Waking through a page change after an idle sleep must stick.

    The idle clock used to stay at the last key press, so the tick right
    after a page or brightness wake saw the deck idle past its limit and
    slept it again at once. Seen on the real deck on 15/09/2026 as a
    scrolling label that never moved: the deck was asleep.
    </summary>
    <remarks>
    This is a regression test for a fault that looked like something else
    entirely. The deck appeared to be awake, because the wake redrew it, and
    then stopped updating, so the visible symptom was an animation that had
    frozen rather than a deck that had gone to sleep.

    The tail of the test is the half that is easy to leave out: after the
    wake, the deck must go back to sleeping on its normal schedule. A fix
    that reset the clock but also stopped the countdown would pass the first
    half and leave a deck that never sleeps again.
    </remarks>
    """
    controller, deck, clock, urls, _ = setup
    controller.start()
    clock.advance(61)
    controller.tick()
    assert controller.asleep
    controller.switch_page("Second")
    assert not controller.asleep
    controller.tick()
    assert not controller.asleep
    clock.advance(30)
    controller.tick()
    assert not controller.asleep
    clock.advance(31)
    controller.tick()
    assert controller.asleep


def test_keepalive_every_twenty_seconds(setup):
    """<summary>
    Pins the keepalive interval: nothing at nineteen seconds, one at
    twenty one.
    </summary>
    <remarks>
    The deck drops its connection if it hears nothing for long enough, and
    an idle deck showing a static page sends nothing on its own. The
    keepalive is one of the proven commands and is the only thing keeping a
    quiet deck attached. Too frequent and it competes with the tiles; too
    rare and the deck disappears mid session and has to be reopened.
    </remarks>
    """
    controller, deck, clock, *_ = setup
    controller.start()
    deck.reset()
    clock.advance(19)
    controller.tick()
    assert ("keepalive",) not in deck.calls
    clock.advance(2)
    controller.tick()
    assert ("keepalive",) in deck.calls


def test_config_file_change_is_picked_up(setup):
    """<summary>
    Pins the running daemon noticing an edited configuration file, applying
    it, and staying on the same page by name.
    </summary>
    <remarks>
    Keeping the page by name rather than by index is the subtle part. A
    reload after the user inserted a page above the current one would
    otherwise jump the deck to a different page, and the user would be left
    pressing keys that now do something else entirely. If the page has gone,
    the controller falls back rather than failing, which is covered
    separately.

    The file timestamp is moved by hand because the fake clock is not the
    one the file system uses. Both the settings and a full redraw of all
    eighteen panels must follow, since anything on the page could have
    changed.
    </remarks>
    """
    controller, deck, clock, urls, path = setup
    controller.start()
    controller.switch_page("Second")
    deck.reset()
    path.write_text(TEXT.replace("brightness = 70", "brightness = 33"), encoding="utf-8")
    import os
    os.utime(path, (clock.mono + 5000, clock.mono + 5000))
    clock.advance(3)
    controller.tick()
    assert controller.config.deck.brightness == 33
    assert ("brightness", 33) in deck.calls
    assert controller.page.name == "Second"  # page kept by name across a reload
    assert len(deck.tiles()) == 18


def test_events_tiles_and_commands_for_the_server(setup):
    """<summary>
    Pins everything the configuration page depends on: tile pictures, the
    event stream, the snapshot, and commands submitted back into the loop.
    </summary>
    <remarks>
    Commands from the page are queued and run on the controller's own thread
    rather than on the web server's, which is what keeps a single owner for
    the deck. A command that raises must be logged and the loop must carry
    on: a failing button on the page cannot be allowed to take the deck
    down, which is why the deliberate division by zero sits between two
    assertions that the loop still works.

    The grid in the snapshot is the firmware's key numbering for the top
    row, ending in the strip, and the page uses it to map a click to a key.
    Unsubscribing dropping the count to zero matters because the hub is fed
    on every redraw: a subscriber left behind after a browser tab closed
    would have events queued at it forever.
    </remarks>
    """
    controller, deck, clock, urls, _ = setup
    events = controller.hub.subscribe()
    controller.start()
    assert controller.tile_image(0, 0) is not None
    assert controller.tile_image(0, 5) is not None
    assert controller.tile_image(1, 4).size == (95, 95)
    types = [events.get_nowait()["type"] for _ in range(events.qsize())]
    assert "tiles" in types

    controller.handle_event(KeyEvent(3, True))
    kinds = [events.get_nowait()["type"] for _ in range(events.qsize())]
    assert kinds[0] == "key" and "page" in kinds and "tiles" in kinds

    snap = controller.snapshot()
    assert snap["page"] == "Second" and snap["pages"] == ["Main", "Second"]
    assert snap["grid"][0] == [13, 10, 7, 4, 1, 16]
    assert snap["keys"][0]["label"] == "Dim" and snap["keys"][0]["action"] == "brightness"

    controller.submit(lambda: controller.set_brightness(value=25))
    controller.submit(lambda: 1 / 0)  # a failing command is logged, not fatal
    controller.tick()
    assert ("brightness", 25) in deck.calls
    assert controller.brightness == 25

    controller.hub.unsubscribe(events)
    assert controller.hub.subscriber_count == 0


def test_hold_key_marks_the_tile_and_stops_on_reload(setup):
    """<summary>
    Pins a hold started from a deck key being marked on the tile, and pins
    every hold being released when the configuration reloads.
    </summary>
    <remarks>
    The release on reload is the important half. A hold is a key held down
    at the operating system level by a background thread; after a reload
    that thread's key may no longer be bound to anything on the deck, so
    nothing could ever switch it off and the user would be left with a key
    stuck down. Stopping them all on reload is the only safe rule.

    The wait loop is there because the hold runs on its own thread, and the
    fifteen redraws are how the deck shows that something is running: the
    whole page is redrawn so the marked key gains its border.
    </remarks>
    """
    controller, deck, clock, urls, path = setup
    presses = []
    controller.runner.holds = __import__("deckplate.holds", fromlist=["HoldManager"]).HoldManager(
        press=lambda c: presses.append(("press", c)), release=lambda c: presses.append(("release", c)),
        log=lambda t: None)
    hold_config = cfg.parse(TEXT + """
[[pages.keys]]
row = 0
column = 1
label = "Hold"
action = { type = "hold", keys = "w", hold_min_ms = 60000, hold_max_ms = 60000, release_min_ms = 0, release_max_ms = 0 }
""", path.parent, path)
    controller.reload(hold_config)
    controller.switch_page("Second")
    deck.reset()
    controller.handle_event(KeyEvent(10, True))  # key 10 is row 0 column 1
    import time as _t
    deadline = _t.monotonic() + 2
    while ("press", "w") not in presses and _t.monotonic() < deadline:
        _t.sleep(0.01)
    assert ("press", "w") in presses
    assert controller.runner.holds.active("w")
    assert len(deck.tiles()) == 15  # keys re-rendered to show the mark
    controller.reload(hold_config)
    assert not controller.runner.holds.active("w")
    assert presses[-1] == ("release", "w")


def test_animated_key_plays_frames_by_time(setup):
    """<summary>
    Pins animation being driven by the clock rather than by the tick rate,
    and pins the read timeout shortening while something is moving.
    </summary>
    <remarks>
    The middle of this test is the valuable part: a tick that arrives
    without enough time having passed must send nothing at all. Sending a
    frame per tick would flood the link with frames the panel cannot show
    and would starve the key presses sharing it.

    The read timeout is how the loop stays responsive without spinning. It
    drops to half a frame while an animation runs, so frames land on time,
    and returns to a quarter of a second when nothing is moving, so an idle
    deck costs nothing. Both values are pinned, along with the animation
    stopping the moment the page showing it is left.
    </remarks>
    """
    controller, deck, clock, urls, path = setup
    animated = cfg.parse(TEXT + """
[[pages.keys]]
row = 1
column = 2
animation = { kind = "spinner", fps = 10 }
""", path.parent, path)
    controller.reload(animated)
    controller.switch_page("Second")  # the added key is on the last page
    assert controller.animator.active
    assert controller.animator.read_timeout_ms() == 50  # half a frame at 10 a second
    deck.reset()
    clock.advance(0.1)
    controller.tick()
    assert ("frame", 1, 2) in deck.calls and ("commit",) in deck.calls
    frames = len([c for c in deck.calls if c[0] == "frame"])
    controller.tick()  # same frame index: nothing more sent
    assert len([c for c in deck.calls if c[0] == "frame"]) == frames
    clock.advance(0.1)
    controller.tick()
    assert len([c for c in deck.calls if c[0] == "frame"]) == frames + 1
    assert controller.snapshot()["keys"][-1]["animated"] is True
    controller.switch_page("Main")
    assert not controller.animator.active
    assert controller.animator.read_timeout_ms() == 250


def test_press_flash_and_restore(setup):
    """<summary>
    Pins the pressed look: drawn immediately on press, cleared by a later
    tick, and never shown for a key with nothing bound to it.
    </summary>
    <remarks>
    The flash is the deck's only acknowledgement that a press registered, so
    it must be queued and committed before the action runs, not after: on a
    key that launches something slow, the action can take a visible moment.

    Restoring is done by the tick, not by a timer, so the test steps the
    clock over the threshold and checks both sides of it. A flash that were
    never cleared would leave a key permanently lit as though it were
    running something. A key with no action gets no flash, since flashing
    would claim something happened when nothing did.
    </remarks>
    """
    controller, deck, clock, urls, path = setup
    controller.start()
    controller.reload(cfg.parse(TEXT.replace("press_flash = false", "press_flash = true"), path.parent, path))
    deck.reset()
    controller.handle_event(KeyEvent(13, True))  # Web key, has an action
    assert deck.calls[0] == ("tile", 0, 0) and ("commit",) in deck.calls  # the flash
    assert controller._flashes
    deck.reset()
    controller.tick()
    assert ("tile", 0, 0) not in deck.calls  # not over yet
    clock.advance(0.2)
    controller.tick()
    assert ("tile", 0, 0) in deck.calls and not controller._flashes  # restored
    controller.handle_event(KeyEvent(1, True))  # nothing configured there: no flash
    assert not controller._flashes


def test_press_flash_can_be_turned_off(setup):
    """<summary>
    Pins the press flash setting as genuinely off, with no redraw at all on
    a press.
    </summary>
    <remarks>
    Off has to mean no picture is sent rather than an unchanged picture
    being sent. Every flash and its restore are two extra tiles over USB per
    press, so a user who turned this off to keep the link clear must
    actually get that.
    </remarks>
    """
    controller, deck, clock, urls, path = setup
    controller.start()
    deck.reset()
    controller.handle_event(KeyEvent(13, True))
    assert not controller._flashes and ("tile", 0, 0) not in deck.calls


def test_only_proven_commands_reach_the_deck(setup):
    """<summary>
    The whole screen picture is never painted: that command disabled a deck.
    </summary>
    <remarks>
    This test must not be weakened. It drives the controller through a
    background colour change, a sleep, a wake, an idle period and several
    ticks, and then asserts that the entire call log contains nothing but
    operations that map onto the seven proven commands. Widening that set to
    make something pass would be exactly the wrong fix.

    The temptation it guards against is real: setting a deck wide background
    looks like a job for the deck's whole screen picture command. That
    command, sent with a wrongly sized picture and followed by a mode
    command, permanently disabled a deck on 10 September 2026. The surround
    is darkened by clearing all keys on connect instead, using CLE, and
    there is deliberately no method for anything else, which is why the
    absence of both names is asserted as well.
    </remarks>
    """
    controller, deck, clock, urls, path = setup
    controller.start()
    controller.reload(cfg.parse(TEXT.replace("[deck]", "[deck]\nbackground = '#ff0000'"), path.parent, path))
    controller.sleep_deck()
    controller.wake()
    clock.advance(60)
    for _ in range(5):
        controller.tick()
    assert not hasattr(deck, "set_background_colour") and not hasattr(controller, "_paint_surround")
    # "clear" is CLE, one of the proven seven. The surround is darkened by
    # clearing all keys on connect, never by painting a whole screen picture.
    assert all(c[0] in {"init", "brightness", "clear", "tile", "frame", "sleep", "commit", "keepalive"} for c in deck.calls), deck.calls


def test_reload_to_fewer_pages_keeps_snapshot_valid(setup):
    """<summary>
    Pins a reload that deletes the current page as falling back to a valid
    one rather than leaving a dangling reference.
    </summary>
    <remarks>
    The user can delete the page the deck is showing, either in the
    configuration page or by editing the file, and the daemon must not be
    holding an index into a list that has shrunk. The snapshot is checked as
    well as the page, because the configuration page reads the index and
    would highlight the wrong entry, or none at all, if the two disagreed.
    </remarks>
    """
    controller, deck, clock, urls, path = setup
    controller.start()
    controller.switch_page("Second")
    one_page = cfg.parse("[[pages]]\nname = 'Only'", path.parent, path)
    controller.reload(one_page)
    snap = controller.snapshot()
    assert snap["page"] == "Only" and snap["page_index"] == 0
    assert controller.page.name == "Only"


def test_run_stops_after_seconds_and_handles_queued_events(setup):
    """<summary>
    Pins the real run loop: it reads events, acts on them, and stops when
    its time is up.
    </summary>
    <remarks>
    Everything else in this file calls tick by hand, so this is the only
    test of the loop that a running daemon actually executes. The read is
    wrapped so that time advances as it is called, which is what lets the
    loop reach its own deadline without the test ever sleeping. A loop that
    ignored its stop condition would hang the suite rather than fail it,
    which is worth knowing if this ever appears to freeze.
    </remarks>
    """
    controller, deck, clock, urls, _ = setup
    controller.start()
    deck.events = [KeyEvent(13, True), KeyEvent(13, False)]
    original_read = deck.read_event

    def read_and_advance(timeout_ms):
        """
        <summary>
        Advance the fake clock a quarter second, then read as the real method would.
        </summary>
        <param name="timeout_ms">Passed straight on.</param>
        <returns>Whatever the real read returns.</returns>
        """
        clock.advance(0.25)
        return original_read(timeout_ms)

    deck.read_event = read_and_advance
    controller.run(seconds=1.0)
    assert urls == ["https://example.org"]


# Preview rendering for the configuration page.
#
# The page shows the panels much larger than the deck does, so it may ask for a
# tile drawn above the deck's image size. The whole point of these tests is that
# doing so never reaches hardware: see docs/hardware-safety.md.

def test_preview_tile_draws_above_the_deck_image_size(setup):
    """<summary>
    Pins the preview renderer producing tiles larger than the deck's own
    image size, at the exact multiple asked for.
    </summary>
    <remarks>
    The configuration page shows the deck several times life size, and
    scaling up a picture already drawn at ninety five pixels would look
    blurred. So the preview renders afresh at the larger size. A scale of
    one must still give exactly the deck's own size, since that is the
    picture the page compares against. The strip is included because it has
    its own visible window and a separate drawing path.
    </remarks>
    """
    controller, deck, *_ = setup
    controller.start()
    native = controller.preview_tile(0, 0, 1)
    assert native.width == deck.image_size

    for scale in (2, 3):
        bigger = controller.preview_tile(0, 0, scale)
        assert bigger.width == bigger.height == deck.image_size * scale

    strip = controller.preview_tile(1, STRIP_COLUMN, 2)
    assert strip.width == deck.image_size * 2


def test_preview_tile_sends_nothing_to_the_deck(setup):
    """<summary>
    Pins preview drawing as completely separate from the deck: no packet
    sent, no cached tile changed, no animation track added.
    </summary>
    <remarks>
    This is a hardware safety test as much as a correctness one. Previews
    are drawn at up to three times the deck's image size, and a picture that
    size reaching the device would be exactly the wrong sized picture that
    contributed to disabling a deck. The empty call log is the assertion
    that matters, and it is checked after sweeping every position at every
    scale.

    The other two assertions guard the leak in the opposite direction. If a
    preview overwrote the cached tile, the deck would later redraw at the
    wrong size from the cache; if it registered an animation track, the
    controller would start playing a loop that exists only for the browser.
    Tiles are compared by their actual bytes, not by identity, so a picture
    redrawn in place is still caught.
    </remarks>
    """
    controller, deck, *_ = setup
    controller.start()
    deck.reset()
    tiles_before = {p: (i.size, i.tobytes()) for p, i in controller._tiles.items()}
    tracks_before = set(controller.animator.tracks)

    for scale in (1, 2, 3):
        for row in range(layout.ROWS):
            for column in range(layout.COLUMNS):
                controller.preview_tile(row, column, scale)

    assert deck.calls == [], "preview drawing must never write to the deck"
    assert set(controller.animator.tracks) == tracks_before
    assert {p: (i.size, i.tobytes()) for p, i in controller._tiles.items()} == tiles_before


def test_preview_scale_is_clamped_to_the_ceiling(setup):
    """<summary>
    Pins an absurd or nonsensical scale as clamped rather than honoured or
    refused.
    </summary>
    <remarks>
    The scale arrives over the network from the configuration page, so it is
    untrusted input. Without a ceiling, a large number would ask for an
    enormous image and exhaust memory in the controller's own process, which
    is the process holding the deck. Zero and negative values are clamped up
    to the native size rather than raising, so a malformed request costs a
    wasted redraw and nothing more.
    </remarks>
    """
    controller, deck, *_ = setup
    controller.start()
    widest = deck.image_size * controller_module.MAX_PREVIEW_SCALE
    assert controller.preview_tile(0, 0, 99).width == widest
    assert controller.preview_tile(0, 0, 0).width == deck.image_size
    assert controller.preview_tile(0, 0, -5).width == deck.image_size


def test_hardware_render_still_uses_the_deck_image_size_exactly(setup):
    """<summary>
    The guarantee the preview path must not erode.
    </summary>
    <remarks>
    Every picture that reaches the deck is exactly the size the deck's own
    spec gives, and the assertion is a set of one, so a single stray size
    fails it. The previews at larger scales are rendered in the middle of
    the test on purpose: they must leave no trace on what the hardware is
    later sent.

    Sending a picture of the wrong size to a deck is not a cosmetic fault.
    A wrongly sized picture written through a whole screen command is
    precisely what disabled a deck, and while the tile path is a different
    command, the rule of never guessing at a size stands regardless.
    </remarks>
    """
    controller, deck, *_ = setup
    controller.start()
    sizes = set()
    original = deck.set_position_image

    def record(row, column, image):
        """
        <summary>
        Note the image size, then pass the call on to the real method.
        </summary>
        <param name="row">Row.</param>
        <param name="column">Column.</param>
        <param name="image">The tile.</param>
        """
        sizes.add(image.size)
        original(row, column, image)

    deck.set_position_image = record
    controller.render_all()
    for scale in (2, 3):
        controller.preview_tile(0, 0, scale)
    assert sizes == {(deck.image_size, deck.image_size)}


def test_preview_scales_the_strip_visible_window(setup):
    """<summary>
    A panel drawn larger must show its content larger, not in a small middle
    box.
    </summary>
    <remarks>
    The visible window is the part of a strip tile the bezel lets through,
    measured in deck pixels. Drawing the tile at twice the size without
    scaling the window would leave the content in a small box in the middle
    of a large picture, which is what the page would then show. None stays
    None, meaning no window was configured, and must not become a scaled
    zero.
    </remarks>
    """
    controller, deck, *_ = setup
    controller.start()
    visible = controller.config.strip_visible
    assert controller._scaled_visible(visible, deck.image_size) == visible
    assert controller._scaled_visible(visible, deck.image_size * 2) == (visible[0] * 2, visible[1] * 2)
    assert controller._scaled_visible(None, deck.image_size * 2) is None


def test_hardware_strip_render_keeps_the_visible_window_unchanged(setup):
    """<summary>
    The scaling above must never alter what the deck itself is sent.
    </summary>
    <remarks>
    The counterpart to the scaling test: the deck always gets its own image
    size and the window exactly as configured. The window is a physical
    measurement of the bezel, so a scaled value reaching the hardware would
    put the content outside what can be seen, and the panel would look
    blank with nothing in the log.

    The module function is replaced for the duration and restored in a
    finally, since leaving a patched drawing function behind would quietly
    affect every test that ran afterwards.
    </remarks>
    """
    controller, deck, *_ = setup
    controller.start()
    seen = []
    original = tiles_module.strip_tile

    def record(tile, now, observation, size, location, visible=None, default_background=None):
        """
        <summary>
        Note the size and visibility asked for, then pass the call on to the real renderer.
        </summary>
        <param name="tile">The tile config.</param>
        <param name="now">The time.</param>
        <param name="observation">Weather reading.</param>
        <param name="size">Pixel size.</param>
        <param name="location">Location text.</param>
        <param name="visible">Visibility flag.</param>
        <param name="default_background">Background colour.</param>
        <returns>The real renderer's result.</returns>
        """
        seen.append((size, visible))
        return original(tile, now, observation, size, location, visible, default_background)

    tiles_module.strip_tile = record
    try:
        controller.render_strip(force=True)
    finally:
        tiles_module.strip_tile = original
    assert seen, "the strip should have been drawn"
    for size, visible in seen:
        assert size == deck.image_size
        assert visible == controller.config.strip_visible


# Long press and double press. The timing itself is tested in test_presses.py;
# what matters here is that the controller feeds the router both halves of the
# report and runs what comes back.

# A configuration whose first key carries a long press and whose second carries
# a double press, with both thresholds set short enough to step over by hand.
SECOND_ACTIONS = """
[deck]
press_flash = false
long_press_ms = 400
double_press_ms = 300

[[pages]]
name = "Main"

[[pages.keys]]
row = 0
column = 0
label = "Web"
action = { type = "url", url = "https://example.org" }
action_long = { type = "url", url = "https://long.example" }

[[pages.keys]]
row = 1
column = 0
label = "Two"
action = { type = "url", url = "https://plain.example" }
action_double = { type = "url", url = "https://double.example" }
"""


@pytest.fixture
def seconds(tmp_path):
    """<summary>
    A controller whose first two keys carry a long press and a double press.
    </summary>
    <returns>The controller, deck, clock and opened URL list, already started.</returns>
    <remarks>
    Configuration watching is switched off here, unlike the main fixture,
    because these tests step the clock in fractions of a second and have no
    interest in the file. The controller is started for them, so each test
    is about the gesture and nothing else.
    </remarks>
    """
    path = tmp_path / "config.toml"
    path.write_text(SECOND_ACTIONS, encoding="utf-8")
    config = cfg.load(path)
    deck = FakeDeck()
    clock = FakeTime()
    urls = []
    weather = WeatherService(config.weather, fetcher=lambda s: Observation(10, 0, "Clear", "sun", "C"),
                             monotonic=lambda: clock.mono)
    controller = Controller(deck, config, now=lambda: clock.wall, monotonic=lambda: clock.mono,
                            weather=weather, log=lambda t: None, watch_config=False)
    controller.runner = ActionRunner(controller, open_url=urls.append, log=lambda t: None)
    controller.start()
    return controller, deck, clock, urls


def test_a_short_press_on_a_long_key_waits_for_the_release(seconds):
    """<summary>
    Pins a key with a long press action as doing nothing until the user lets
    go, then running the plain action.
    </summary>
    <remarks>
    This delay is the cost of having a long press at all: until the key is
    released, nobody can tell which of the two gestures it is. The
    assertion that nothing has happened on the press itself is what stops
    the plain action firing first and the long one firing on top of it,
    which would give the user both.
    </remarks>
    """
    controller, deck, clock, urls = seconds
    controller.handle_event(KeyEvent(13, True))   # row 0 column 0
    assert urls == []                             # nothing yet: it might become a long press
    clock.advance(0.1)
    controller.handle_event(KeyEvent(13, False))
    assert urls == ["https://example.org"]


def test_holding_a_key_fires_the_long_action_without_waiting_for_the_release(seconds):
    """<summary>
    Pins the long press firing as soon as the threshold passes, while the
    key is still down, and pins the release adding nothing.
    </summary>
    <remarks>
    Firing at the threshold rather than on release is what makes a long
    press feel like one: the user gets the response at the moment they have
    held long enough, and knows to let go. Waiting for the release would
    leave them holding a key with no feedback at all.

    The release then has to be swallowed, or the plain action would follow
    the long one and the user would get both.
    </remarks>
    """
    controller, deck, clock, urls = seconds
    controller.handle_event(KeyEvent(13, True))
    clock.advance(0.2)
    controller.tick()
    assert urls == []
    clock.advance(0.3)
    controller.tick()
    assert urls == ["https://long.example"]
    controller.handle_event(KeyEvent(13, False))  # and the release adds nothing
    assert urls == ["https://long.example"]


def test_the_loop_wakes_up_in_time_for_a_long_press(seconds):
    """<summary>
    Pins the read timeout shrinking to the long press threshold while a
    gesture is in flight.
    </summary>
    <remarks>
    The long press is fired by a tick, and ticks only happen when the event
    read returns. With the ordinary quarter second timeout the threshold
    would be noticed late, and the delay would drift with the timeout rather
    than with the setting. Nothing here raises if it is wrong: the long
    press simply feels sluggish and inconsistent, which is why the timeout
    is pinned directly.
    </remarks>
    """
    controller, deck, clock, urls = seconds
    timeouts = []
    deck.read_event = lambda timeout_ms: timeouts.append(timeout_ms)
    controller.handle_event(KeyEvent(13, True))
    controller.run(stop=lambda: bool(timeouts))
    assert timeouts and timeouts[0] <= 400


def test_two_quick_presses_are_a_double(seconds):
    """<summary>
    Pins two quick presses as the double action only, with the plain action
    dropped rather than run as well.
    </summary>
    <remarks>
    The tail of the test is what proves the drop. After the double has run,
    time is advanced past the double press window and a tick is taken: if
    the first press had been held pending rather than cancelled, the plain
    action would surface here and the user would get both. For a pair of URL
    keys that means an unwanted extra browser tab every time.
    </remarks>
    """
    controller, deck, clock, urls = seconds
    controller.handle_event(KeyEvent(14, True))   # row 1 column 0
    controller.handle_event(KeyEvent(14, False))
    clock.advance(0.1)
    controller.handle_event(KeyEvent(14, True))
    controller.handle_event(KeyEvent(14, False))
    assert urls == ["https://double.example"]
    clock.advance(1.0)
    controller.tick()
    assert urls == ["https://double.example"]     # the plain action was dropped


def test_one_press_on_a_double_key_runs_the_plain_action_once_the_window_closes(seconds):
    """<summary>
    Pins a single press on a double press key as held back until the window
    closes, then run.
    </summary>
    <remarks>
    The other side of the double press bargain: the plain action on such a
    key is always late by the length of the window, because until it expires
    a second press could still arrive. The two ticks either side of the
    threshold pin both that it waits and that it does eventually fire, and
    the second is the one that would catch a press dropped entirely.
    </remarks>
    """
    controller, deck, clock, urls = seconds
    controller.handle_event(KeyEvent(14, True))
    controller.handle_event(KeyEvent(14, False))
    clock.advance(0.2)
    controller.tick()
    assert urls == []
    clock.advance(0.2)
    controller.tick()
    assert urls == ["https://plain.example"]


def test_a_plain_key_is_not_delayed(setup):
    """<summary>
    The keys nobody has given a second action to behave exactly as before.
    </summary>
    <remarks>
    This uses the ordinary fixture, whose keys carry no long or double press
    at all, and asserts the action ran on the press itself with no clock
    advance and no tick. Almost every key on almost every deck is this kind,
    so adding the gesture machinery must not have made the common case wait
    for anything.
    </remarks>
    """
    controller, deck, clock, urls, _ = setup
    controller.start()
    controller.handle_event(KeyEvent(13, True))
    assert urls == ["https://example.org"]


def test_a_page_switch_drops_a_gesture_in_flight(seconds):
    """<summary>
    Pins a page change as cancelling a press that has not yet resolved.
    </summary>
    <remarks>
    The pending press belongs to the key as it was on the old page. After a
    change, that position carries a different key with a different action,
    so resolving the gesture would run something the user never pressed.
    Dropping it costs one ignored press at the moment of a page change,
    which is the right trade.
    </remarks>
    """
    controller, deck, clock, urls = seconds
    controller.handle_event(KeyEvent(13, True))
    controller.switch_page("Main")
    clock.advance(1.0)
    controller.tick()
    controller.handle_event(KeyEvent(13, False))
    assert urls == []


# Per application pages


# Three pages: one plain, and two tied to windows by a pattern, with focus
# following switched on so the controller actually consults the watcher.
FOLLOW = """
[deck]
press_flash = false
follow_focus = true

[[pages]]
name = "Main"

[[pages]]
name = "Browser"
match_window = "firefox"

[[pages]]
name = "Editor"
match_window = "code|gedit"
"""


class FakeWatcher:
    """<summary>
    Stands in for the focus watcher: a window the test sets by hand.
    </summary>
    <remarks>
    No thread, no xprop and no desktop. The test writes ``current``
    directly, which is exactly how the real watcher publishes what its poll
    last saw, so the controller cannot tell the difference. The start and
    stop counts are kept so a test can prove the watcher was never started
    when the configuration did not ask for it.
    </remarks>
    """

    def __init__(self):
        """
        <summary>
        Start unstarted, with no current window.
        </summary>
        """
        self.current = None
        self.started = 0
        self.stopped = 0
        self.poll_seconds = 0.2

    def start(self):
        """
        <summary>
        Count the start and return self, as the real watcher does.
        </summary>
        <returns>self.</returns>
        """
        self.started += 1
        return self

    def stop(self):
        """
        <summary>
        Count the stop.
        </summary>
        """
        self.stopped += 1


@pytest.fixture
def following(tmp_path):
    """<summary>
    A started controller with focus following switched on and a hand driven
    watcher behind it.
    </summary>
    <returns>The controller, the fake watcher and the clock.</returns>
    """
    path = tmp_path / "config.toml"
    path.write_text(FOLLOW, encoding="utf-8")
    config = cfg.load(path)
    deck = FakeDeck()
    clock = FakeTime()
    watcher = FakeWatcher()
    weather = WeatherService(config.weather, fetcher=lambda s: Observation(10, 0, "Clear", "sun", "C"),
                             monotonic=lambda: clock.mono)
    controller = Controller(deck, config, now=lambda: clock.wall, monotonic=lambda: clock.mono,
                            weather=weather, log=lambda t: None, watch_config=False, watcher=watcher)
    controller.start()
    return controller, watcher, clock


def test_the_deck_follows_the_focused_window(following):
    """<summary>
    Pins the deck changing page as the focused window changes, and pins the
    watcher being started exactly once.
    </summary>
    <remarks>
    Matching is done against the application and the title together, and the
    pattern may list alternatives, which is why the editor page names two
    applications. Started exactly once matters because starting is called
    from more than one place: a watcher started twice would leave a thread
    polling the desktop forever with nothing able to stop it.
    </remarks>
    """
    controller, watcher, clock = following
    assert watcher.started == 1
    watcher.current = focus.Window(app="firefox", title="Deckplate on GitHub")
    controller.tick()
    assert controller.page.name == "Browser"
    watcher.current = focus.Window(app="code", title="controller.py")
    controller.tick()
    assert controller.page.name == "Editor"


def test_a_window_nothing_matches_falls_back_to_the_first_plain_page(following):
    """<summary>
    Pins an unmatched window as returning the deck to the first page that is
    not tied to an application.
    </summary>
    <remarks>
    Staying on the last matched page would leave the user looking at the
    browser keys while working in something else entirely, and pressing
    them would drive the wrong application. The fall back is the first page
    with no window pattern, so a configuration should always keep one such
    page as its general purpose home.
    </remarks>
    """
    controller, watcher, clock = following
    watcher.current = focus.Window(app="firefox")
    controller.tick()
    assert controller.page.name == "Browser"
    watcher.current = focus.Window(app="nautilus", title="Home")
    controller.tick()
    assert controller.page.name == "Main"


def test_a_page_chosen_by_hand_stays_until_the_window_changes(following):
    """<summary>
    Pins a manual page choice as beating focus following until the focused
    window actually changes.
    </summary>
    <remarks>
    Without this, choosing a page by hand while focus following is on would
    be undone by the very next tick, a fraction of a second later, and the
    deck would look as though it had ignored the press entirely. Two ticks
    are taken to prove it is not merely a one tick reprieve.

    The last step is the release condition: the same application with a
    different title still counts as a change of window, so the user is not
    stuck on their manual choice for the rest of the session.
    </remarks>
    """
    controller, watcher, clock = following
    watcher.current = focus.Window(app="firefox")
    controller.tick()
    assert controller.page.name == "Browser"
    controller.switch_page("Editor")
    controller.tick()
    controller.tick()
    assert controller.page.name == "Editor"
    watcher.current = focus.Window(app="firefox", title="a different tab")
    controller.tick()
    assert controller.page.name == "Browser"


def test_nothing_is_watched_when_the_config_does_not_ask(setup):
    """<summary>
    Pins the watcher as never started when the configuration has not asked
    for focus following.
    </summary>
    <remarks>
    A watcher is attached here and must still sit untouched. Starting it
    anyway would poll the desktop several times a second for the life of the
    daemon, and would run xprop on machines where the user never wanted it,
    all to feed a feature that is switched off.
    </remarks>
    """
    controller, deck, clock, urls, _ = setup
    watcher = FakeWatcher()
    controller.watcher = watcher
    controller.start()
    controller.tick()
    assert watcher.started == 0


# One animated key with a long scrolling label and one plain key, for the
# preview and event tests that need a page where exactly one thing moves.
ANIMATED_TEXT = """
[deck]
brightness = 70
press_flash = false

[[pages]]
name = "Main"

[[pages.keys]]
row = 0
column = 0
label = "Request Hangar Access"
animation = { kind = "scroll", speed = 1, fps = 20 }

[[pages.keys]]
row = 1
column = 1
label = "Still"
"""


def test_preview_animation_is_a_gif_of_the_loop_and_never_touches_the_deck(tmp_path):
    """<summary>
    The page asks for the whole loop of an animated panel; a still one gets
    nothing.
    </summary>
    <remarks>
    Three separate guarantees sit in here. The GIF is the whole loop at the
    scale asked for, so the browser can play it without asking for frames.
    A key that does not animate, and a position with no key at all, both
    answer None rather than a one frame GIF, which is what stops the page
    showing a video player over a still picture.

    Nothing reaches the deck, checked by the call count either side, for the
    same reason as the other preview tests: this renders at twice the deck's
    image size.

    The caching is pinned by identity, not equality, so the second request
    must return the very same object rather than an equal one. Rebuilding a
    long loop on every request would stall the controller thread that also
    owns the deck. The reload then has to drop it, or an edited animation
    would keep showing its old self in the page with no way to refresh.
    </remarks>
    """
    import io
    from PIL import Image
    path = tmp_path / "config.toml"
    path.write_text(ANIMATED_TEXT, encoding="utf-8")
    config = cfg.load(path)
    deck = FakeDeck()
    clock = FakeTime()
    controller = Controller(deck, config, now=lambda: clock.wall, monotonic=lambda: clock.mono,
                            log=lambda t: None)
    controller.start()
    assert controller.animated_positions() == [[0, 0]]
    assert controller.snapshot()["animated"] == [[0, 0]]
    sent_before = len(deck.calls)
    data = controller.preview_animation(0, 0, scale=2)
    assert data is not None and data[:6] in (b"GIF89a", b"GIF87a")
    with Image.open(io.BytesIO(data)) as gif:
        assert gif.is_animated and gif.size == (190, 190)
    assert controller.preview_animation(1, 1, scale=2) is None
    assert controller.preview_animation(2, 2, scale=1) is None
    assert len(deck.calls) == sent_before, "a preview reached the deck"
    # the same loop comes back from the cache, and a change to the key drops it
    assert controller.preview_animation(0, 0, scale=2) is data
    path.write_text(ANIMATED_TEXT.replace("speed = 1", "speed = 2"), encoding="utf-8")
    controller.reload(cfg.load(path))
    assert controller.preview_animation(0, 0, scale=2) is not data


def test_tiles_events_carry_the_animated_positions(tmp_path):
    """<summary>
    Pins every tiles event naming which positions animate.
    </summary>
    <remarks>
    The configuration page decides from this whether to request a still
    picture or the animated preview for each panel. Sending the list on
    every tiles event, rather than once at connect, is what lets the page
    react to a reload that made a key start or stop moving. A page that
    missed it would keep showing a frozen first frame where the deck is
    animating.
    </remarks>
    """
    path = tmp_path / "config.toml"
    path.write_text(ANIMATED_TEXT, encoding="utf-8")
    config = cfg.load(path)
    deck = FakeDeck()
    clock = FakeTime()
    controller = Controller(deck, config, now=lambda: clock.wall, monotonic=lambda: clock.mono,
                            log=lambda t: None)
    seen = controller.hub.subscribe()
    controller.start()
    events = []
    while not seen.empty():
        events.append(seen.get_nowait())
    tiles_events = [e for e in events if e["type"] == "tiles"]
    assert tiles_events and all(e["animated"] == [[0, 0]] for e in tiles_events)


def test_wallpaper_is_cut_per_key_at_the_pitch_and_scales_for_the_page(tmp_path):
    """<summary>
    Every key gets its own window onto the page picture; a bigger preview
    scales the pitch too.
    </summary>
    <remarks>
    A page wallpaper is one picture spread across all fifteen keys, so each
    key must be cut from the right place. The picture is a left to right
    gradient precisely so the cut can be checked by colour: a key further
    right must be bluer, which catches a cut that is mirrored or that uses
    the firmware's key numbering instead of the visible grid.

    The exact comparison against the slicing helper is what pins the offset
    rather than merely the direction. The preview at twice the size must
    scale the pitch with it, or the larger drawing would show a different
    part of the picture from the one on the deck, and the page and the
    hardware would visibly disagree.
    </remarks>
    """
    from PIL import Image, ImageChops
    picture = Image.new("RGB", (600, 200))
    for x in range(600):
        picture.paste((int(255 * (1 - x / 599)), 0, int(255 * x / 599)), (x, 0, x + 1, 200))
    wall = tmp_path / "bg.png"
    picture.save(wall)
    text = f"""
[deck]
brightness = 70
press_flash = false
key_pitch_x = 142
key_pitch_y = 158

[[pages]]
name = "Main"
wallpaper = "{wall}"

[[pages.keys]]
row = 0
column = 0
label = "Web"
action = {{ type = "url", url = "https://example.org" }}
"""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    config = cfg.load(path)
    deck = FakeDeck()
    clock = FakeTime()
    controller = Controller(deck, config, now=lambda: clock.wall, monotonic=lambda: clock.mono,
                            log=lambda t: None)
    controller.start()
    left = controller.tile_image(0, 1)
    right = controller.tile_image(0, 4)
    assert left is not None and right is not None
    assert left.getpixel((10, 40))[2] < right.getpixel((10, 40))[2]  # bluer to the right
    expected = tiles_module.wallpaper_slice(wall, 95, (142, 158), 0, 1)
    assert ImageChops.difference(left, expected).getbbox() is None
    # the page asks for a bigger drawing: same window, pitch scaled with it
    big = controller.preview_tile(0, 1, scale=2)
    assert ImageChops.difference(big, tiles_module.wallpaper_slice(wall, 190, (284, 316), 0, 1)).getbbox() is None
    assert controller.snapshot()["key_pitch"] == [142, 158]
    assert controller.snapshot()["wallpaper"] == str(wall)


# Four keys covering the running border rules: a hold and a repeat left on
# their automatic styles, a repeat forced steady and red, and a hold with the
# border switched off entirely.
TOGGLES = """
[deck]
brightness = 70

[[pages]]
name = "Main"

[[pages.keys]]
row = 0
column = 0
action = { type = "hold", keys = "w" }

[[pages.keys]]
row = 0
column = 1
action = { type = "repeat", keys = "f", every_ms = 1000 }

[[pages.keys]]
row = 0
column = 2
action = { type = "repeat", keys = "g", every_ms = 1000 }
mark = { style = "steady", colour = "#ff0000" }

[[pages.keys]]
row = 0
column = 3
action = { type = "hold", keys = "s" }
mark = { style = "none" }
"""


def _toggles_controller(tmp_path):
    """<summary>
    A controller over the toggling key configuration, not yet started.
    </summary>
    <returns>The controller, deck and clock.</returns>
    <remarks>
    Deliberately not started, because two of the tests below inspect the
    border rules directly without ever drawing anything. The ones that need
    a drawn page start it themselves.
    </remarks>
    """
    path = tmp_path / "config.toml"
    path.write_text(TOGGLES, encoding="utf-8")
    config = cfg.load(path)
    deck = FakeDeck()
    clock = FakeTime()
    weather = WeatherService(config.weather, fetcher=lambda s: Observation(10, 0, "Clear", "sun", "C"),
                             monotonic=lambda: clock.mono)
    controller = Controller(deck, config, now=lambda: clock.wall, monotonic=lambda: clock.mono,
                            weather=weather, log=lambda t: None)
    controller.runner = ActionRunner(controller, open_url=lambda u: None, log=lambda t: None)
    return controller, deck, clock


def test_the_border_style_and_colour_follow_the_key(tmp_path):
    """<summary>
    Auto blinks a repeat and holds a hold; a key may force either, or none.
    </summary>
    <remarks>
    The automatic styles mirror what the key is doing, which is the whole
    idea: a hold is continuously down, so its border is steady, while a
    repeat is tapping, so its border blinks. Both are sampled on each half
    of the blink phase, which is what tells a steady border from a blinking
    one.

    A key may override the style and the colour, and a style of none must
    mean no border at any phase, for someone who finds a lit key
    distracting. The final line is the rule that outranks all of them:
    nothing is marked when nothing is running, whatever the style says,
    because the border means running and must never mean anything else.
    </remarks>
    """
    controller, _deck, clock = _toggles_controller(tmp_path)
    keys = controller.page.keys
    running = {"w", "repeat:f", "repeat:g", "s"}
    positions = [(0, 0), (0, 1), (0, 2), (0, 3)]
    half = cfg.MARK_DEFAULT_FLASH_MS / 1000
    while not controller._flash_phase(cfg.MARK_DEFAULT_FLASH_MS):
        clock.advance(half)
    on = {p: controller._key_mark(keys[p], running) for p in positions}
    clock.advance(half)                      # the other half of the blink
    off = {p: controller._key_mark(keys[p], running) for p in positions}

    assert on[(0, 0)] is not None and off[(0, 0)] is not None      # hold: steady
    assert on[(0, 1)] is not None and off[(0, 1)] is None          # repeat: blinks
    assert on[(0, 2)] == (255, 0, 0) and off[(0, 2)] == (255, 0, 0)  # forced steady, red
    assert on[(0, 3)] is None and off[(0, 3)] is None              # style "none": never
    # nothing running, nothing marked, whatever the style says
    assert all(controller._key_mark(keys[p], set()) is None for p in positions)


def test_a_key_may_blink_at_its_own_rate(tmp_path):
    """<summary>
    flash_ms is per key, so two keys can blink at different speeds.
    </summary>
    <remarks>
    The phase is computed from the clock and the key's own period rather
    than from a shared counter, which is what allows two keys to blink at
    different rates at the same time. The clock is pinned to a known value
    first, because the test compares phases either side of one advance and a
    starting point mid flip would make the slower key appear to have
    changed.
    </remarks>
    """
    controller, _deck, clock = _toggles_controller(tmp_path)
    clock.mono = 100.0
    # 500 ms halves flip every half second; 250 ms halves flip twice as often
    slow_first, fast_first = controller._flash_phase(500), controller._flash_phase(250)
    clock.advance(0.25)
    assert controller._flash_phase(500) == slow_first          # slow has not flipped
    assert controller._flash_phase(250) != fast_first          # fast has


def test_blinking_redraws_only_the_repeating_key(tmp_path):
    """<summary>
    A blink costs one picture, not the whole page, and stops when the task
    does.
    </summary>
    <remarks>
    A blinking border is the only thing on the deck that repaints
    indefinitely while the user does nothing, so its cost is pinned exactly.
    One tile per half phase, and that tile is the repeating key alone: a
    redraw of the whole page here would put fifteen pictures over the USB
    link every half second, for one border.

    The tail is the part that would otherwise go unnoticed. Once the task is
    switched off, exactly one more redraw is allowed, to take the border
    away, and then nothing at all however long the loop runs. A version that
    kept redrawing a key with no border on it would look perfectly correct
    on screen while quietly saturating the link forever.
    </remarks>
    """
    controller, deck, clock = _toggles_controller(tmp_path)
    controller.start()
    controller.runner.holds.active_combos = lambda: {"repeat:f"}
    deck.reset()
    controller.tick()                      # border comes on
    assert deck.tiles() == [("tile", 0, 1)]
    deck.reset()
    clock.advance(controller_module.MARK_FLASH_SECONDS)      # other half: border off again
    controller.tick()
    assert deck.tiles() == [("tile", 0, 1)]
    deck.reset()
    clock.advance(controller_module.MARK_FLASH_SECONDS)      # and back on
    controller.tick()
    assert deck.tiles() == [("tile", 0, 1)]
    # Switched off while the border is showing: one redraw clears it, and then
    # nothing keeps going out, however long the loop runs.
    controller.runner.holds.active_combos = lambda: set()
    deck.reset()
    controller.tick()
    assert deck.tiles() == [("tile", 0, 1)]
    for _ in range(4):
        deck.reset()
        clock.advance(controller_module.MARK_FLASH_SECONDS)
        controller.tick()
        assert deck.tiles() == []


def test_a_held_key_does_not_blink(tmp_path):
    """<summary>
    A steady border is drawn once and then left alone, whatever the phase.
    </summary>
    <remarks>
    The counterpart to the blinking test, and the cheaper of the two: a
    steady border costs exactly one picture for the whole time the hold
    runs. Four phase boundaries are stepped over with nothing sent, which is
    what proves the redraw is driven by the border actually changing rather
    than by the phase ticking over.
    </remarks>
    """
    controller, deck, clock = _toggles_controller(tmp_path)
    controller.start()
    controller.runner.holds.active_combos = lambda: {"w"}   # the hold key
    deck.reset()
    controller.tick()
    assert deck.tiles() == [("tile", 0, 0)]                 # border on, once
    for _ in range(4):
        deck.reset()
        clock.advance(controller_module.MARK_FLASH_SECONDS)
        controller.tick()
        assert deck.tiles() == []                           # and it stays put
