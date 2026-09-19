"""<summary>
The daemon loop: config in, pictures out, key presses to actions.
</summary>
<remarks>
One Controller drives one deck. It renders the current page and the display
strip, listens for key events, runs actions, keeps the clock and weather
tiles fresh, sleeps the deck after idle, and reloads the config file when it
changes on disk. Time sources and the deck are injectable for the tests.

Threading is the thing to understand before changing anything here. Only the
loop thread may touch the deck. The HTTP server runs on its own threads and
never draws: it reads the shared state under a lock, or posts work through
<see cref="Controller.submit"/> to be run on the loop thread at the next
tick. Drawing from a server thread would interleave packets with the loop's
own and put half a page on the screens.

Nothing here sends a command of its own. Every byte that reaches the deck
goes through the Deck class and its gate, which is what keeps this file to
the seven proven commands however much is added to it. The preview calls,
which draw panels far larger than the deck can show, are deliberately built
so that nothing they produce can reach the hardware: see
<see cref="Controller.preview_tile"/> and docs/hardware-safety.md.
</remarks>
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import replace
from datetime import datetime
from typing import Callable

from . import animations, audio, focus, holds, images, layout, live, tiles
from .actions import ActionRunner
from .config import MARK_DEFAULT_FLASH_MS, POSITIONAL_ACTIONS, Action, Config, load
from .presses import Actions, PressRouter
from .protocol import KeyEvent
from .weather import WeatherService

# How often the handshake goes out. The deck only needs to hear from the host
# now and then, and letting it lapse stops key presses being reported while
# the deck still looks alive, so this is the one timer that must keep running
# even while the deck is asleep.
KEEPALIVE_SECONDS = 20
# Actions that switch something on and off rather than firing once. A key
# bound to one of these carries a border while its task is running.
TOGGLING_ACTIONS = ("hold", "boost", "repeat")
# A key that is holding something down keeps a steady border. A repeat is not
# holding anything, it fires now and then, so its border blinks instead and the
# two read differently across the deck. Each key may set its own style, colour
# and rate; this is how long each half of the default blink lasts.
MARK_FLASH_SECONDS = MARK_DEFAULT_FLASH_MS / 1000
# How often the config file's timestamp is checked, how long a press flash
# stays on the key, and how long the loop may sit waiting on the deck with
# nothing else to do. The read timeout is the loop's heartbeat: everything
# that has to happen on time, an animation frame, a blink, a long press, is
# allowed to shorten it, never to lengthen it. See Controller.run.
CONFIG_POLL_SECONDS = 2
FLASH_SECONDS = 0.12
IDLE_READ_MS = 250
# How far above the deck's own image size the configuration page may ask a panel
# to be drawn. The page shows the panels much larger than the deck does, and a
# tile drawn at the deck's size and blown up in the browser looks soft. Drawing
# is not free, so this is a ceiling rather than an invitation. It bounds only
# what the page is served: nothing drawn above the deck's image size is ever
# sent to hardware. See Controller.preview_tile.
MAX_PREVIEW_SCALE = 3
# Encoded preview loops kept for the page, one per position and scale.
PREVIEW_GIF_CACHE = 32
# How often the sound system is asked whether it is muted and which output is
# in use, while the page showing has a key whose face depends on the answer.
# Each ask is a process on Linux, so this is seconds, not milliseconds.
AUDIO_POLL_SECONDS = 3
# How long the loop may sit on the deck while a timer or stopwatch is showing
# on the page, so its digits change on time.
LIVE_READ_MS = 200


class Track:
    """<summary>
    One animated panel: its frames as device ready JPEGs and where it is in
    the loop.
    </summary>
    <remarks>
    The frames are encoded once, when the track is made, and replayed from
    then on. That is the whole point of keeping them: encoding fifteen
    panels afresh every frame would not keep up, and the deck is given the
    same bytes each time round the loop anyway.

    ``last`` is the frame index the deck was last given, and -1 means it has
    been given nothing. Setting it back to -1 is how something that drew
    over a panel asks for the current frame to be sent again.
    </remarks>
    """

    def __init__(self, jpegs: list[bytes], fps: int, start: float) -> None:
        """<summary>Hold one panel's encoded frames and start its clock.</summary>
        <param name="jpegs">Frames already encoded at the deck's image size.</param>
        <param name="fps">Frames a second. Floored at 1, so a config asking
        for none still plays rather than dividing by zero.</param>
        <param name="start">The monotonic time the loop is measured from.</param>
        """
        self.jpegs = jpegs
        self.fps = max(1, fps)
        self.start = start
        self.last = -1

    def index(self, now: float) -> int:
        """<summary>Which frame is due at this moment.</summary>
        <param name="now">Monotonic time, from the same source as ``start``.</param>
        <returns>An index into ``jpegs``, wrapping round for ever.</returns>
        <remarks>Worked out from the clock rather than counted up, so a slow
        tick drops frames instead of playing the whole loop late.</remarks>"""
        return int((now - self.start) * self.fps) % len(self.jpegs)


class Animator:
    """<summary>
    Plays animated panels by time, so frame rate does not depend on the loop
    rate.
    </summary>
    <remarks>
    Owns every panel that is moving, keyed by (row, column). A panel with a
    track here belongs to the animator: anything else that draws over it
    must call <see cref="restart"/> rather than assume its own picture will
    stay, because the next frame will overwrite it otherwise.

    Nothing is committed here. <see cref="tick"/> queues the frames that are
    due and says whether it queued any, and the loop commits once for
    everything that changed in that pass, animation and strip together.
    </remarks>
    """

    def __init__(self, deck, monotonic: Callable[[], float]) -> None:
        """<summary>Start with nothing playing.</summary>
        <param name="deck">The deck to draw on. Only ever used from the loop
        thread.</param>
        <param name="monotonic">The clock, injectable so the tests can run a
        long animation in no time at all.</param>
        """
        self.deck = deck
        self.monotonic = monotonic
        self.tracks: dict[tuple[int, int], Track] = {}

    def set(self, row: int, column: int, frames: list, fps: int) -> None:
        """<summary>Give a panel an animation, replacing whatever it had.</summary>
        <param name="row">Row from 0 at the top.</param>
        <param name="column">Column from 0 at the left, or the strip column.</param>
        <param name="frames">Pillow images, in order.</param>
        <param name="fps">Frames a second to play them at.</param>
        <remarks>
        Encodes every frame here and now, which is the expensive part and
        why this is called when a page is built rather than while it plays.
        The loop starts from the current time, so a panel begins at its
        first frame whenever it is set.
        </remarks>
        """
        size = self.deck.image_size
        jpegs = [images.encode(frame, size) for frame in frames]
        self.tracks[(row, column)] = Track(jpegs, fps, self.monotonic())

    def clear(self, row: int, column: int) -> None:
        """<summary>Stop animating one panel, if it was.</summary>
        <remarks>Leaves whatever frame is on the screens showing. The caller
        redraws the panel if it wants something else there.</remarks>"""
        self.tracks.pop((row, column), None)

    def clear_keys(self) -> None:
        """<summary>Stop animating the keys, leaving the display strip playing.</summary>
        <remarks>
        The split is deliberate. A page change replaces every key but the
        strip belongs to the config rather than to the page, so its clock or
        weather panel carries on across a page switch without a stutter.
        Use <see cref="clear_all"/> for a config reload or a wake, where the
        strip goes too.
        </remarks>"""
        for position in [p for p in self.tracks if p[1] != layout.STRIP_COLUMN]:
            del self.tracks[position]

    def clear_all(self) -> None:
        """<summary>Stop animating everything, keys and strip alike.</summary>"""
        self.tracks.clear()

    def restart(self, row: int, column: int) -> None:
        """<summary>
        Force the next tick to resend the current frame, after something drew
        over it.
        </summary>
        <remarks>
        The press flash is what needs this. Without it the animator sees the
        frame it wanted already sent, sends nothing, and the flash stays on
        the key until the loop happens to come round to the next frame.
        Does not move the animation on: the same frame is simply sent again.
        </remarks>"""
        track = self.tracks.get((row, column))
        if track is not None:
            track.last = -1

    @property
    def active(self) -> bool:
        """<summary>Is anything animating at all?</summary>
        <returns>True while at least one panel has a track.</returns>"""
        return bool(self.tracks)

    def read_timeout_ms(self) -> int:
        """<summary>
        How long the loop may block on the deck: short while something is
        animating.
        </summary>
        <returns>Milliseconds, never above <see cref="IDLE_READ_MS"/> and
        never below 15.</returns>
        <remarks>
        Half the fastest frame interval, so a frame is never a whole frame
        late. The floor of 15 stops a config asking for an absurd frame rate
        turning the loop into a busy wait, and the ceiling is what lets an
        idle deck sit quietly waiting for a key.
        </remarks>"""
        if not self.tracks:
            return IDLE_READ_MS
        fastest = max(track.fps for track in self.tracks.values())
        return max(15, min(IDLE_READ_MS, int(1000 / fastest / 2)))

    def tick(self) -> bool:
        """<summary>
        Queue every frame that is due.
        </summary>
        <returns>True if anything was queued, so the caller knows a commit is
        needed.</returns>
        <remarks>
        Sends nothing for a panel already showing the right frame, which is
        the usual case: the loop runs faster than the animation on purpose.
        Does not commit, because the loop commits once for the whole pass.
        </remarks>"""
        now = self.monotonic()
        sent = False
        for (row, column), track in self.tracks.items():
            index = track.index(now)
            if index != track.last:
                self.deck.set_position_jpeg(row, column, track.jpegs[index])
                track.last = index
                sent = True
        return sent


class EventHub:
    """<summary>
    Fan out events to any number of subscribers, each with its own queue.
    </summary>
    <remarks>
    A slow subscriber never blocks the controller: when its queue is full the
    event is dropped for that subscriber only.

    That is the whole design. The controller publishes from the loop thread
    and must never wait on a browser, so an event stream that has stopped
    being read loses events rather than stalling the deck. Subscribers are
    therefore written to cope with a gap: the page asks for a fresh snapshot
    when it reconnects rather than assuming it saw everything.

    Every event is the same dict object handed to each queue. Nothing may
    edit one after publishing it.
    </remarks>
    """

    def __init__(self, capacity: int = 200) -> None:
        """<summary>Start with no subscribers.</summary>
        <param name="capacity">How many events one subscriber may fall behind
        by before its events start being dropped.</param>
        """
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self.capacity = capacity

    def subscribe(self) -> queue.Queue:
        """<summary>Take out a subscription and get the queue it feeds.</summary>
        <returns>A fresh queue, already receiving.</returns>
        <remarks>
        Every subscriber must pass its queue back to
        <see cref="unsubscribe"/> when it is finished, or the hub goes on
        filling a queue nobody reads for as long as the daemon runs. The
        server does that in a finally around each event stream.
        </remarks>"""
        subscription: queue.Queue = queue.Queue(maxsize=self.capacity)
        with self._lock:
            self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: queue.Queue) -> None:
        """<summary>End a subscription. Harmless if it has already ended.</summary>
        <param name="subscription">The queue handed out by
        <see cref="subscribe"/>.</param>
        <remarks>Anything still sitting in the queue is left there for the
        caller to drain or drop as it sees fit.</remarks>"""
        with self._lock:
            if subscription in self._subscribers:
                self._subscribers.remove(subscription)

    def publish(self, event: dict) -> None:
        """<summary>Offer one event to every current subscriber.</summary>
        <param name="event">The event. Shared, not copied, so do not change
        it afterwards.</param>
        <remarks>
        Never blocks and never raises, which is what makes it safe to call
        from the middle of the loop. The subscriber list is copied under the
        lock and the queues are filled outside it, so a subscriber joining
        or leaving cannot deadlock against a publish.
        </remarks>"""
        with self._lock:
            targets = list(self._subscribers)
        for target in targets:
            try:
                target.put_nowait(event)
            except queue.Full:
                pass

    @property
    def subscriber_count(self) -> int:
        """<summary>How many subscriptions are open right now.</summary>
        <returns>The count, usually one browser tab each.</returns>
        <remarks>A snapshot taken under the lock, and stale the moment it is
        returned. For the tests and for reporting, not for deciding
        anything.</remarks>"""
        with self._lock:
            return len(self._subscribers)


class Controller:
    """<summary>
    One deck, driven: the current page on its screens, its keys wired to
    actions, and everything the configuration page needs to watch it.
    </summary>
    <remarks>
    Built, then started, then run. <see cref="start"/> puts the first page on
    the screens and <see cref="run"/> blocks until it is told to stop, so a
    controller that has been built but not started has drawn nothing and sent
    nothing.

    The loop thread owns the deck. Everything reachable from the HTTP server
    is either read under a lock, <see cref="snapshot"/> and
    <see cref="tile_image"/>, or posted through <see cref="submit"/> to run
    on the loop thread. A method that draws is a loop thread method, with the
    preview pair the deliberate exception: they draw into a returned image
    and send nothing.

    The deck, the clocks, the action runner, the weather service, the log and
    the focus watcher are all injected, so the whole of this file is tested
    with no hardware, no network and no desktop in sight.
    </remarks>
    """

    def __init__(self, deck, config: Config, *,
                 runner: ActionRunner | None = None,
                 weather: WeatherService | None = None,
                 now: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print,
                 watch_config: bool = True,
                 watcher: "focus.FocusWatcher | None" = None) -> None:
        """<summary>
        Wire a deck to a config, without drawing anything yet.
        </summary>
        <param name="deck">An open deck, already initialised or not:
        <see cref="start"/> initialises it either way.</param>
        <param name="config">The config to drive from. Replaced wholesale by
        <see cref="reload"/> rather than edited in place.</param>
        <param name="runner">What runs the actions. Made here if not given.</param>
        <param name="weather">The weather service. Made here if not given, in
        which case it fetches nothing until the first refresh is due.</param>
        <param name="now">Wall clock, for the clock tiles.</param>
        <param name="monotonic">Elapsed time, for every timer here. Kept
        separate from ``now`` so that the clock going back, over a summer time
        change or an NTP correction, cannot make a timer wait for ever.</param>
        <param name="log">Where progress lines go.</param>
        <param name="watch_config">Whether to poll the config file for
        changes. Ignored when the config did not come from a file.</param>
        <param name="watcher">The focus watcher. Made here if not given, and
        not started unless the config asks to follow the focused window.</param>
        <remarks>
        Nothing is sent to the deck from here. That matters for the tests and
        for the configuration page, both of which build a controller before
        anything is ready to be drawn.
        </remarks>
        """
        self.deck = deck
        self.config = config
        self.now = now
        self.monotonic = monotonic
        self.log = log
        self.runner = runner or ActionRunner(self, log=log)
        self.weather = weather or WeatherService(config.weather, monotonic=monotonic, log=log)
        self.page_index = 0
        self.brightness = config.deck.brightness
        self.asleep = False
        self.last_activity = monotonic()
        self.last_keepalive = monotonic()
        self.last_config_poll = monotonic()
        self.watch_config = watch_config and config.path is not None
        self.config_mtime = self._config_mtime()
        self._strip_signatures: list[tuple | None] = [None] * layout.ROWS
        # Shared with the HTTP server thread: events out, commands in, tiles for the page.
        self.hub = EventHub()
        self.commands: queue.Queue[Callable[[], None]] = queue.Queue()
        self._tiles: dict[tuple[int, int], object] = {}
        self._tiles_lock = threading.Lock()
        self.animator = Animator(deck, monotonic)
        self._preview_gifs: dict[tuple[int, int, int], tuple[tuple, bytes]] = {}
        self._preview_lock = threading.Lock()
        self._flashes: dict[tuple[int, int], float] = {}
        # The face last drawn on each key (its border, whether it is active,
        # and any live text and ring), so only the keys whose face actually
        # changed are redrawn while one blinks or counts.
        self._marks: dict[tuple[int, int], tuple | None] = {}
        # What the live keys remember: toggles, counters, timers, stopwatches.
        self.states = live.KeyStates()
        # Whether the sound is muted, which output is in use and what outputs
        # exist, as last asked, and when. Asked only while a key needs it.
        self._audio_status: tuple[bool | None, str | None, list] = (None, None, [])
        self._audio_polled: float | None = None
        # Set while a clock is showing on the page, to keep the loop awake.
        self._live_ms: int | None = None
        # The shortest blink half period running now, so the loop can keep up
        # with a key set to flash faster than the idle read timeout.
        self._blink_ms: int | None = None
        # Long press and double press timing. See deckplate/presses.py.
        self.router = PressRouter(long_ms=config.deck.long_press_ms,
                                  double_ms=config.deck.double_press_ms)
        # Per application pages. The watcher is only started when the config
        # asks for it, so nothing polls the desktop on a deck that does not
        # follow the focused window.
        self.watcher = watcher if watcher is not None else focus.FocusWatcher(
            config.deck.focus_poll_ms / 1000, log=log)
        self._focus_signature: tuple[str, str] | None = None

    # Lifecycle

    @property
    def page(self):
        """<summary>The page being shown now.</summary>
        <returns>The Page at the current index.</returns>
        <remarks>
        Reads two fields that a reload changes one after the other, so it is
        only safe on the loop thread. A server thread takes its own local
        copies and guards the index instead: see <see cref="snapshot"/>.
        </remarks>"""
        return self.config.pages[self.page_index]

    def start(self) -> None:
        """<summary>
        Initialise the deck, blank it, and put the first page up.
        </summary>
        <remarks>
        The order is not interchangeable. The deck is initialised before
        anything is drawn, the screens are blanked before the tiles go on
        top, and the weather is refreshed before the first render so the
        strip is not drawn once empty and again a moment later.

        Call this once, before <see cref="run"/>. Calling it twice is
        harmless but redraws everything for no reason; a wake is
        <see cref="wake"/>, which does the same work and knows about the
        idle clock.
        </remarks>"""
        self.deck.initialise()
        self.deck.set_brightness(self.brightness)
        self._blank_screen()
        self.weather.refresh_if_due()
        self.render_all()
        self.log(f"showing page '{self.page.name}' with {len(self.page.keys)} keys")
        if self.config.deck.follow_focus:
            self.watcher.start()

    def run(self, seconds: float | None = None, stop: Callable[[], bool] = lambda: False) -> None:
        """<summary>
        Block and drive the deck until stop() is true or the time is up.
        </summary>
        <param name="seconds">How long to run for, or None to run until
        stopped. The tests use a short run; the daemon passes None.</param>
        <param name="stop">Asked once each time round, before the deck is read.
        Return True to come out of the loop.</param>
        <remarks>
        The loop spends nearly all its life blocked on
        <see cref="deck.read_event"/>, and the timeout it blocks for is how
        everything else gets to happen on time. Each thing that is due soon,
        an animation frame, a blinking border, a press flash ending, a long
        press firing, shortens that timeout and nothing lengthens it, so the
        loop wakes for whichever is soonest and sleeps the rest of the time.

        <see cref="Controller.stop"/> runs in a finally, so held keys are released and
        the focus watcher is stopped however the loop ends, including on an
        unplug or a Ctrl+C.

        A deliberately stopped run leaves the last page on the screens. The
        deck keeps showing it after the daemon exits, which is intended.
        </remarks>
        """
        deadline = None if seconds is None else self.monotonic() + seconds
        try:
            while not stop():
                if deadline is not None and self.monotonic() >= deadline:
                    break
                timeout = IDLE_READ_MS if self.asleep else self.animator.read_timeout_ms()
                if not self.asleep and self._blink_ms:
                    timeout = min(timeout, max(30, self._blink_ms // 2))
                if not self.asleep and self._live_ms:
                    timeout = min(timeout, self._live_ms)
                if self._flashes:
                    timeout = min(timeout, 30)
                # A long press has to fire on time, and the loop would otherwise
                # be asleep on the deck for a quarter of a second.
                due = self.router.due(self.monotonic())
                if due is not None:
                    timeout = min(timeout, max(5, int(due * 1000)))
                event = self.deck.read_event(timeout)
                if event is not None:
                    self.handle_event(event)
                self.tick()
        finally:
            self.stop()

    # Rendering

    def _blank_screen(self) -> None:
        """<summary>
        Clear every key and commit, so the whole screen goes dark before the
        tiles are drawn on top.
        </summary>
        <remarks>
        The keys are regions of one screen, and the surround around them is the
        part of that screen no tile covers. After a USB drop it can come back
        light. A USB capture of the official app shows it clears all keys on
        connect (CLE with 0xFF) before it redraws, which is what puts the
        surround back to dark. This does the same, using the proven CLE command
        only.
        </remarks>
        """
        self.deck.clear()
        self.deck.commit()

    def _refresh_marks(self) -> None:
        """<summary>
        Redraw only the keys whose face changed: a border that came or went,
        a toggle that flipped, a timer whose digits moved, a mute that ended.
        </summary>
        <remarks>
        A repeat blinks, so its key needs redrawing twice a second while it
        runs, and a timer changes every second. Sending the whole page that
        often would be fifteen pictures a time, so only the keys whose face
        actually changed go out. An animated key with nothing live on it is
        left to the animator, which owns what is on it.

        Runs on every tick, which is why it has to be cheap when nothing is
        happening: with nothing running, every key wants the face it already
        has, and nothing at all is sent. It is also what clears a border the
        moment its task stops, so nothing else has to notice that a hold has
        ended.
        </remarks>
        """
        holding = self.runner.holds.active_combos()
        size = self.deck.image_size
        self._blink_ms = self._blinking_ms(holding)
        self._live_ms = LIVE_READ_MS if self.states.any_running(self.page.name) else None
        sent = False
        for row in range(layout.ROWS):
            for column in range(layout.LCD_COLUMNS):
                want = self._face_signature(row, column, holding)
                if self._marks.get((row, column)) == want:
                    continue
                self._marks[(row, column)] = want
                image, frames, fps = self._draw_key(row, column, size, holding)
                if frames is not None:
                    # A live value on an animated key: the frames carry the
                    # text and the ring, so the animator gets a new loop.
                    self.animator.set(row, column, frames, fps)
                elif (row, column) in self.animator.tracks:
                    continue
                self.deck.set_position_image(row, column, image)
                self._remember_tile(row, column, image)
                sent = True
        if sent:
            self.deck.commit()
            self.hub.publish({"type": "tiles", "page": self.page.name,
                              "columns": list(range(layout.LCD_COLUMNS)),
                              "animated": self.animated_positions()})

    def render_all(self) -> None:
        """<summary>Redraw the keys and the strip, and commit once.</summary>
        <remarks>
        One commit for the whole screen on purpose: committing after the
        keys and again after the strip shows the page half built. Used for a
        page that is entirely new, a start, a wake or a reload. For the
        ordinary tick the two are refreshed separately and far more
        cheaply.
        </remarks>"""
        self.render_keys(commit=False)
        self.render_strip(force=True, commit=False)
        self.deck.commit()

    def _flash_phase(self, flash_ms: int) -> bool:
        """<summary>
        Which half of the blink we are in. Each key blinks on its own clock.
        </summary>
        <param name="flash_ms">How long one half of this key's blink lasts.</param>
        <returns>True for the half that shows the border.</returns>
        <remarks>
        Worked out from the clock rather than counted, so two keys set to
        the same rate blink together however long each has been running,
        and a slow tick cannot leave a key stuck on.
        </remarks>"""
        return int(self.monotonic() / (max(1, flash_ms) / 1000)) % 2 == 0

    def _key_mark(self, key, holding):
        """<summary>
        The running border colour for this key right now, or None for none.
        </summary>
        <param name="key">The key to judge, or None for an empty position.</param>
        <param name="holding">The task keys running now, from the action
        runner.</param>
        <returns>A colour, or None for no border at all. None is also what a
        blinking key returns for the dark half of its blink, which is how the
        blink is drawn without a second code path.</returns>
        <remarks>
        The key's own mark settings decide it. "auto" blinks a repeat, which
        only fires now and then, and keeps a steady border for a hold or a
        boost, which are holding a key down. "steady" and "blink" force one or
        the other and "none" leaves the key alone. Any of the key's three
        actions counts, since the toggle may be on the long or double press.

        The first running action wins: a key whose press and long press both
        toggle something shows one border, not two.
        </remarks>
        """
        if key is None:
            return None
        settings = key.mark
        if settings.style == "none":
            return None
        for action in key.actions():
            if action is None or action.type not in TOGGLING_ACTIONS:
                continue
            if holds.task_key(action.type, action.params) not in holding:
                continue
            blinks = settings.style == "blink" or (settings.style == "auto"
                                                   and action.type == "repeat")
            if blinks and not self._flash_phase(settings.flash_ms):
                return None
            return images.colour(settings.colour, tiles.HOLDING)
        return None

    def _blinking_ms(self, holding) -> int | None:
        """<summary>
        The shortest blink half period in use, so the loop can keep up.
        </summary>
        <param name="holding">The task keys running now, from the action
        runner.</param>
        <returns>Milliseconds, or None when nothing on this page is
        blinking.</returns>
        <remarks>
        Without this the loop would sit on the deck for a quarter of a
        second and a key set to blink faster than that would stutter. Only
        keys that are actually running something count, so a page full of
        fast blink settings costs nothing until one of them is pressed.
        </remarks>"""
        shortest = None
        for key in self.page.keys.values():
            if key.mark.style == "none":
                continue
            for action in key.actions():
                if action is None or action.type not in TOGGLING_ACTIONS:
                    continue
                if holds.task_key(action.type, action.params) not in holding:
                    continue
                if key.mark.style == "blink" or (key.mark.style == "auto"
                                                 and action.type == "repeat"):
                    shortest = key.mark.flash_ms if shortest is None else min(shortest, key.mark.flash_ms)
                break
        return shortest

    def _audio_state(self) -> tuple[bool | None, str | None, list]:
        """<summary>
        Whether the sound is muted, which output is in use and the outputs
        there are, asked of the sound system no more than every few seconds.
        </summary>
        <returns>(muted, default output id, outputs). Unknown parts are None
        or empty, which draws the key as not active.</returns>
        <remarks>
        Asked lazily, so a page with no key that cares costs nothing, and
        never more often than AUDIO_POLL_SECONDS, because on Linux each ask
        is a pactl process. Any failure is swallowed into an unknown answer:
        a machine with no sound control still draws its keys, just without
        the muted face.
        </remarks>
        """
        now = self.monotonic()
        if self._audio_polled is not None and now - self._audio_polled < AUDIO_POLL_SECONDS:
            return self._audio_status
        self._audio_polled = now
        try:
            backend = self.runner.audio
            self._audio_status = (backend.is_muted(), backend.default_output(), backend.outputs())
        except Exception:  # no pactl, no pycaw, or a refusal: the face simply shows nothing
            self._audio_status = (None, None, [])
        return self._audio_status

    def _key_face(self, key, position: tuple[int, int], holding, page) -> tuple[bool, "live.Face | None"]:
        """<summary>
        Whether a key is active right now, and the live face it shows if any.
        </summary>
        <param name="key">The key's config, or None for an empty position.</param>
        <param name="position">Row and column, for the state store.</param>
        <param name="holding">The task keys running now, from the action runner.</param>
        <param name="page">The page the key is on, for the state store's key.</param>
        <returns>(active, face). Active picks the key's active picture and
        label; the face carries a timer's, stopwatch's or counter's text
        and ring, or is None for a key with none.</returns>
        <remarks>
        Any of the key's three actions can make it active: a toggle that is
        on, a hold, boost or repeat that is running, a timer or stopwatch
        that is going, a mute key while the sound is muted, an output key
        while its output is the one in use. The first live face found wins,
        so a key with a timer on its press and a counter on its double press
        shows the timer.

        A timer or stopwatch that has never been pressed still has a face,
        its length or 0:00, so the key reads as what it is before first use.
        </remarks>
        """
        if key is None:
            return False, None
        at = (page.name, position[0], position[1])
        state = self.states.peek(at)
        now = self.monotonic()
        active = False
        face = None
        for action in key.actions():
            if action is None:
                continue
            kind = action.type
            if kind == "toggle":
                active = active or self.states.is_on(at)
            elif kind in TOGGLING_ACTIONS:
                active = active or holds.task_key(kind, action.params) in holding
            elif kind in ("timer", "stopwatch") and not action.params.get("reset"):
                if isinstance(state, live.ClockState):
                    shown = state.face(now)
                elif kind == "timer":
                    shown = live.Face(text=live.format_seconds(action.params["seconds"]), ring=1.0)
                else:
                    shown = live.Face(text="0:00")
                face = face or shown
                active = active or shown.active
            elif kind == "counter" and not action.params.get("reset"):
                count = state.count if isinstance(state, live.CounterState) else 0
                face = face or live.Face(text=str(count))
            elif kind == "volume" and "mute" in action.params:
                muted, _default, _outputs = self._audio_state()
                active = active or bool(muted)
            elif kind == "audio_output" and "device" in action.params:
                _muted, default, outputs = self._audio_state()
                chosen = audio.pick_output(outputs, action.params["device"])
                active = active or (chosen is not None and chosen.id == default)
        return active, face

    def _face_signature(self, row: int, column: int, holding) -> tuple:
        """<summary>
        Everything that decides how a key is drawn right now, as one comparable value.
        </summary>
        <param name="row">Row from 0 at the top.</param>
        <param name="column">Column from 0 at the left.</param>
        <param name="holding">The task keys running now.</param>
        <returns>(border colour, active, face).</returns>
        <remarks>Compared with the last one drawn to decide whether the key
        needs sending again. A face changes once a second at most, since its
        text is whole seconds and its ring is rounded to the pixel.</remarks>
        """
        key = self.page.keys.get((row, column))
        mark = self._key_mark(key, holding)
        active, face = self._key_face(key, (row, column), holding, self.page)
        if face is not None and face.ring is not None:
            face = replace(face, ring=round(face.ring * 100) / 100)
        return mark, active, face

    def _draw_key(self, row: int, column: int, size: int, holding, page=None, config=None) -> tuple:
        """<summary>
        Draw one key panel at any size, without sending anything.
        </summary>
        <param name="row">Row from 0 at the top.</param>
        <param name="column">Column from 0 at the left.</param>
        <param name="size">Pixel size to draw at. The deck's own image size for
        anything going to hardware; a multiple of it for the configuration page,
        which shows the panels far larger than the deck does.</param>
        <param name="holding">Hold combos currently down, from the action runner.</param>
        <param name="page">Page to read the key from. Defaults to the live one; an
        HTTP thread passes its own copy, as <see cref="snapshot"/> does.</param>
        <param name="config">Config to read the default background from, same reason.</param>
        <returns>(image, frames, fps). frames is None when the panel is still.</returns>
        <remarks>
        Drawing only. It sends nothing, caches nothing and touches neither the
        deck nor the animator, so the preview path can call it at any size
        without a single byte reaching the hardware.
        </remarks>
        """
        page = self.page if page is None else page
        config = self.config if config is None else config
        key = page.keys.get((row, column))
        mark = self._key_mark(key, holding)
        active, face = self._key_face(key, (row, column), holding, page)
        if active and key is not None and (key.image_active or key.label_active):
            key = replace(key, image=key.image_active or key.image, label=key.label_active or key.label)
        default_bg = config.deck.background
        ring_colour = None
        if face is not None:
            ring_colour = tiles.ALERT if face.alert else images.ACCENT
        animated = tiles.key_frames(key if face is None else replace(key, label=None), size,
                                    active=mark is not None, default_background=default_bg, mark_colour=mark)
        if animated is not None:
            frames, fps = animated
            if face is not None and face.text:
                frames = [tiles.with_label(frame, face.text) for frame in frames]
                if face.ring is not None:
                    frames = [tiles.with_ring(frame, face.ring, ring_colour) for frame in frames]
            return frames[0], frames, fps
        backdrop = None
        if page.wallpaper is not None:
            # The pitch is measured in the deck's own pixels; a page preview
            # drawn larger scales it with the size so the slices still line up.
            pitch = config.deck.key_pitch
            if pitch is not None and size != self.deck.image_size:
                factor = size / self.deck.image_size
                pitch = (round(pitch[0] * factor), round(pitch[1] * factor))
            backdrop = tiles.wallpaper_slice(page.wallpaper, size, pitch, row, column)
        if face is not None and face.text:
            tile = tiles.live_tile(key, size, face.text, face.ring, ring_colour,
                                   default_background=default_bg, backdrop=backdrop)
            if mark is not None:
                tile = tiles.with_mark(tile, mark)
            return tile, None, 0
        return tiles.key_tile(key, size, active=mark is not None, default_background=default_bg,
                              backdrop=backdrop, mark_colour=mark), None, 0

    def _draw_strip(self, row: int, size: int, now=None, observation=None, config=None) -> tuple:
        """<summary>
        Draw one display strip panel at any size, without sending anything.
        </summary>
        <param name="row">Which strip panel, from 0 at the top.</param>
        <param name="size">Pixel size to draw at, as for <see cref="_draw_key"/>.</param>
        <param name="now">Wall clock time for a clock panel. Defaults to
        now.</param>
        <param name="observation">Weather to show. Defaults to the latest,
        which may be None when nothing has been fetched yet.</param>
        <param name="config">Config to read the strip from. Defaults to the
        live one; an HTTP thread passes its own copy.</param>
        <returns>(image, frames, fps). frames is None when the panel is still.</returns>
        <remarks>
        Drawing only, with the same guarantee as <see cref="_draw_key"/>.

        strip_visible is the window of the panel the hardware actually shows,
        in pixels out of the deck's image size. It has to grow with the drawing
        size or a panel drawn larger puts its clock in a small box in the middle
        of a big one. At the deck's own size the factor is 1 and this changes
        nothing, which is what keeps the hardware path identical.
        </remarks>
        """
        config = self.config if config is None else config
        tile = config.strip[row]
        now = self.now() if now is None else now
        observation = self.weather.observation if observation is None else observation
        default_bg = config.deck.background
        visible = self._scaled_visible(config.strip_visible, size)
        animated = tiles.strip_frames(tile, size, visible, default_bg)
        if animated is not None:
            frames, fps = animated
            return frames[0], frames, fps
        image = tiles.strip_tile(tile, now, observation, size,
                                 config.weather.location_name, visible,
                                 default_bg)
        return image, None, 0

    def _scaled_visible(self, visible, size: int):
        """<summary>
        The strip's visible window, in proportion to the size being drawn.
        </summary>
        <param name="visible">The window at the deck's own image size, or
        None when the whole panel is visible.</param>
        <param name="size">The size being drawn at.</param>
        <returns>The window unchanged when drawing at the deck's own image size.</returns>
        <remarks>
        The strip panel is larger than the part of it the hardware shows, so
        everything is laid out inside that window. Scaling it with the
        drawing size is what stops a preview putting a small clock in the
        middle of a big panel.
        </remarks>
        """
        native = self.deck.image_size
        if visible is None or size == native or not native:
            return visible
        factor = size / native
        return (round(visible[0] * factor), round(visible[1] * factor))

    def render_keys(self, commit: bool = True) -> None:
        """<summary>
        Draw and send every key of the current page.
        </summary>
        <param name="commit">Commit at the end. Pass False when the strip is
        going out in the same pass, so the whole screen appears at once.</param>
        <remarks>
        The expensive call: fifteen panels drawn, encoded and sent. Use it
        for a page that has genuinely changed, not for a border or a clock.
        <see cref="_refresh_marks"/> and <see cref="render_strip"/> are the
        cheap paths and are what the tick uses.

        Key animations are cleared and rebuilt here, and any press flash on
        a key is dropped, because the key it was brightening is being
        replaced anyway. Flashes on the strip survive, since the strip is
        not touched.
        </remarks>"""
        size = self.deck.image_size
        holding = self.runner.holds.active_combos()
        self.animator.clear_keys()
        self._flashes = {p: t for p, t in self._flashes.items() if p[1] == layout.STRIP_COLUMN}
        for row in range(layout.ROWS):
            for column in range(layout.LCD_COLUMNS):
                image, frames, fps = self._draw_key(row, column, size, holding)
                if frames is not None:
                    self.animator.set(row, column, frames, fps)
                self.deck.set_position_image(row, column, image)
                self._remember_tile(row, column, image)
                self._marks[(row, column)] = self._face_signature(row, column, holding)
        if commit:
            self.deck.commit()
        self.hub.publish({"type": "tiles", "page": self.page.name, "columns": list(range(layout.LCD_COLUMNS)),
                          "animated": self.animated_positions()})

    def render_strip(self, force: bool = False, commit: bool = True) -> bool:
        """<summary>
        Send only the strip tiles whose content changed.
        </summary>
        <param name="force">Send every panel whatever its signature says.
        Needed after a wake or a reload, where the screens no longer hold
        what the signatures claim.</param>
        <param name="commit">Commit at the end when anything was sent.</param>
        <returns>True if anything was sent.</returns>
        <remarks>
        Called on every tick, so the signature check is what keeps a clock
        panel to one redraw a minute rather than several a second. A panel
        whose content has not changed costs a signature and nothing else.
        </remarks>"""
        now = self.now()
        observation = self.weather.observation
        sent = False
        for row, tile in enumerate(self.config.strip):
            signature = tiles.strip_signature(tile, now, observation)
            if not force and signature == self._strip_signatures[row]:
                continue
            image, frames, fps = self._draw_strip(row, self.deck.image_size, now, observation)
            if frames is not None:
                self.animator.set(row, layout.STRIP_COLUMN, frames, fps)
            else:
                self.animator.clear(row, layout.STRIP_COLUMN)
            self.deck.set_position_image(row, layout.STRIP_COLUMN, image)
            self._remember_tile(row, layout.STRIP_COLUMN, image)
            self._strip_signatures[row] = signature
            sent = True
        if sent and commit:
            self.deck.commit()
        if sent:
            self.hub.publish({"type": "tiles", "page": self.page.name, "columns": [layout.STRIP_COLUMN],
                              "animated": self.animated_positions()})
        return sent

    def _remember_tile(self, row: int, column: int, image) -> None:
        """
        <summary>
        Cache the image last sent to a position, under the tiles lock, so previews and the web page can read it.
        </summary>
        <param name="row">Row.</param>
        <param name="column">Column.</param>
        <param name="image">The tile as sent.</param>
        """
        with self._tiles_lock:
            self._tiles[(row, column)] = image

    def animated_positions(self) -> list[list[int]]:
        """<summary>
        Which panels are animating, for the configuration page.
        </summary>
        <returns>Every [row, column] that is playing an animation, keys and
        strip alike, sorted. Lists rather than tuples because this goes
        straight out as JSON.</returns>
        <remarks>The page uses this to ask for a moving preview of those panels only.</remarks>"""
        return [[row, column] for row, column in sorted(self.animator.tracks)]

    def tile_image(self, row: int, column: int):
        """<summary>
        A copy of the last upright image rendered for that panel, or None.
        </summary>
        <param name="row">Row from 0 at the top.</param>
        <param name="column">Column from 0 at the left, or the strip column.</param>
        <returns>A fresh copy, or None when that panel has not been drawn
        yet.</returns>
        <remarks>
        Safe from any thread, which is the point: the server serves panel
        pictures from here rather than drawing its own. A copy rather than
        the image itself, so a caller that edits what it gets back, the
        press flash does exactly that, cannot corrupt what the loop thinks
        is on the screens.

        Upright, not in the deck's own orientation. The rotating for the
        hardware happens on the way out, in the images module.
        </remarks>"""
        with self._tiles_lock:
            image = self._tiles.get((row, column))
        return image.copy() if image is not None else None

    def preview_tile(self, row: int, column: int, scale: int = 1):
        """<summary>
        Draw a panel larger than the deck needs, for the configuration page.
        </summary>
        <param name="row">Row from 0 at the top.</param>
        <param name="column">Column from 0 at the left, or the strip column.</param>
        <param name="scale">Multiple of the deck's image size to draw at, 1 to
        <see cref="MAX_PREVIEW_SCALE"/>. 1 returns the cached hardware tile
        unchanged, which is what the page asks for when it is showing the panels
        at or below their real size.</param>
        <returns>An upright image, or None when nothing has been drawn yet.</returns>
        <remarks>
        THIS NEVER REACHES THE DECK. It draws into a fresh image and returns it:
        nothing is sent, nothing is committed, the animator is untouched and the
        cache of what the hardware was actually given is left alone. The deck
        only ever receives images at its own image size, drawn by
        <see cref="render_keys"/> and <see cref="render_strip"/>, and that must
        stay true: see docs/hardware-safety.md.
        </remarks>
        """
        scale = max(1, min(int(scale), MAX_PREVIEW_SCALE))
        if scale == 1:
            return self.tile_image(row, column)
        # Local copies, so a config reload part way through cannot be seen half
        # applied. Same reasoning as snapshot().
        config = self.config
        index = min(self.page_index, len(config.pages) - 1)
        page = config.pages[index]
        size = self.deck.image_size * scale
        if column == layout.STRIP_COLUMN:
            image, _, _ = self._draw_strip(row, size, config=config)
        else:
            holding = self.runner.holds.active_combos()
            image, _, _ = self._draw_key(row, column, size, holding, page=page, config=config)
        return image

    def preview_animation(self, row: int, column: int, scale: int = 1) -> bytes | None:
        """<summary>
        The whole loop of an animated panel as a GIF, for the configuration
        page.
        </summary>
        <param name="row">Row from 0 at the top.</param>
        <param name="column">Column from 0 at the left, or the strip column.</param>
        <param name="scale">Multiple of the deck's image size to draw at, as for
        <see cref="preview_tile"/>.</param>
        <returns>GIF bytes, or None when that panel is not animated.</returns>
        <remarks>
        THIS NEVER REACHES THE DECK, for the same reasons as
        <see cref="preview_tile"/>: it draws afresh and encodes, touching neither
        the animator nor the cache of hardware tiles. The GIF is remembered per
        position and scale until the panel's definition changes, because a long
        scroll is a few hundred frames and the page asks again on every event.
        </remarks>
        """
        scale = max(1, min(int(scale), MAX_PREVIEW_SCALE))
        config = self.config
        index = min(self.page_index, len(config.pages) - 1)
        page = config.pages[index]
        size = self.deck.image_size * scale
        if column == layout.STRIP_COLUMN:
            if not (0 <= row < len(config.strip)):
                return None
            signature = (repr(config.strip[row]), config.strip_visible, config.deck.background)
            frames_of = lambda: self._draw_strip(row, size, config=config)
        else:
            key = page.keys.get((row, column))
            holding = self.runner.holds.active_combos()
            signature = (repr(key), config.deck.background, tuple(sorted(holding)))
            frames_of = lambda: self._draw_key(row, column, size, holding, page=page, config=config)
        cache_key = (row, column, scale)
        with self._preview_lock:
            cached = self._preview_gifs.get(cache_key)
            if cached is not None and cached[0] == signature:
                return cached[1]
        _, frames, fps = frames_of()
        if frames is None:
            return None
        encoded = animations.encode_gif(frames, fps)
        with self._preview_lock:
            if len(self._preview_gifs) >= PREVIEW_GIF_CACHE:
                self._preview_gifs.clear()
            self._preview_gifs[cache_key] = (signature, encoded)
        return encoded

    # Shared state for the HTTP server

    def submit(self, command: Callable[[], None]) -> None:
        """<summary>
        Queue work to run on the controller thread at the next tick.
        </summary>
        <param name="command">Something taking no arguments. Its return value
        is discarded and an exception from it is logged and swallowed.</param>
        <remarks>
        The way in for every HTTP request that changes anything. A server
        thread must never draw on the deck itself, so it posts the work here
        and answers the request straight away.

        Nothing is waited for and nothing comes back. A request that needs
        an answer reads <see cref="snapshot"/> instead, or waits for the
        event the command will publish when it runs.
        </remarks>"""
        self.commands.put(command)

    def _run_commands(self) -> None:
        """<summary>
        Run everything the server threads have queued, then return.
        </summary>
        <remarks>
        Drains the queue rather than taking one command a tick, so a burst
        from the page is applied in one pass. A command that raises is
        logged and the rest still run: a bad request must never stop the
        loop and take the deck with it.
        </remarks>"""
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                command()
            except Exception as err:  # a bad request must not stop the loop
                self.log(f"command failed: {err}")

    def snapshot(self) -> dict:
        """<summary>
        Everything the configuration page needs to draw itself, as plain
        JSON ready data.
        </summary>
        <returns>A fresh dict: pages, keys, strip, device, weather, focus and
        the current state of the deck.</returns>
        <remarks>
        Called from HTTP threads, so it takes local copies of the config and
        the page index and guards the index rather than trusting
        <see cref="page"/>: a reload part way through would otherwise index
        a shorter page list.

        Only plain types go in it, and paths are turned into text, because
        this is handed straight to a JSON encoder. It is a picture of one
        moment and is out of date as soon as it is returned, which is why
        the page also subscribes to the event stream rather than polling
        this.
        </remarks>
        """
        config = self.config
        index = min(self.page_index, len(config.pages) - 1)
        page = config.pages[index]
        keys = []
        for (row, column), key in sorted(page.keys.items()):
            keys.append({
                "row": row, "column": column,
                "label": key.label,
                "image": str(key.image) if key.image else None,
                "action": key.action.type if key.action else None,
                "action_long": key.action_long.type if key.action_long else None,
                "action_double": key.action_double.type if key.action_double else None,
                "animated": (row, column) in self.animator.tracks,
            })
        return {
            "page": page.name,
            "page_index": index,
            "pages": [p.name for p in config.pages],
            "keys": keys,
            "strip": [tile.kind for tile in config.strip],
            "animated": self.animated_positions(),
            "key_pitch": list(config.deck.key_pitch) if config.deck.key_pitch else None,
            "wallpaper": str(page.wallpaper) if page.wallpaper else None,
            "strip_visible": {"width": config.strip_visible[0], "height": config.strip_visible[1]},
            "brightness": self.brightness,
            "asleep": self.asleep,
            "focus": {
                "following": config.deck.follow_focus,
                "window": str(self.watcher.current) if self.watcher.current else None,
            },
            "device": {
                "name": self.deck.spec.name,
                "usb_id": self.deck.spec.usb_id,
                "image_size": self.deck.image_size,
                # How far above image_size the page may ask a panel to be drawn,
                # so it does not have to hardcode the daemon's ceiling.
                "max_preview_scale": MAX_PREVIEW_SCALE,
            },
            "grid": layout.grid(),
            "config_path": str(config.path) if config.path else None,
            "weather": {
                "location": config.weather.location_name,
                "temperature": self.weather.observation.temperature_text if self.weather.observation else None,
                "label": self.weather.observation.label if self.weather.observation else None,
            },
        }

    # Events

    def handle_event(self, event: KeyEvent) -> None:
        """<summary>
        Take one press or release report from the deck and act on it.
        </summary>
        <param name="event">One report, already decoded by the protocol
        module.</param>
        <remarks>
        A press on a sleeping deck wakes it and does nothing else, so the
        key someone reaches for to wake the deck does not also fire. A press
        on the display strip is reported to the page and otherwise ignored:
        the strip is a display, not a row of buttons.

        Releases used to be thrown away. They are not any more: a key with a
        long press or a double press action cannot be settled until the key
        comes back up, and that timing is worked out by <see cref="router"/>.
        A key with neither still fires on the press, with nothing added to the
        path between the press and the action.
        </remarks>
        """
        row, column = layout.position(event.key)
        self.hub.publish({"type": "key", "key": event.key, "row": row, "column": column,
                          "pressed": event.pressed})
        if not event.pressed:
            self._run_actions(self.router.release(row, column, self.monotonic()))
            return
        self.last_activity = self.monotonic()
        if self.asleep:
            self.wake()
            return
        if layout.is_strip(event.key):
            return
        key = self.page.keys.get((row, column))
        if key is None:
            return
        actions = Actions(*self._bind(key, row, column))
        if not actions.any:
            return
        if self.config.deck.press_flash:
            self._flash(row, column)
        self._run_actions(self.router.press(row, column, actions, self.monotonic()))

    def _bind(self, key, row: int, column: int):
        """<summary>
        Give the positional actions among a key's three the key they sit on,
        and give a stopwatch key its unasked for reset on the long press.
        </summary>
        <param name="key">The key's config.</param>
        <param name="row">Row from 0 at the top.</param>
        <param name="column">Column from 0 at the left.</param>
        <returns>The plain, long and double actions, with each toggle, timer,
        stopwatch or counter copied to carry ``at``: the page name, row and
        column.</returns>
        <remarks>
        The router hands actions back long after the press, on a release or
        a tick, with no memory of which key they came from, so the position
        travels with the action. It is a runtime copy: the config's own
        Action objects are never written to, and the document form never
        sees the extra key.

        A stopwatch that runs and pauses on the press has nothing else its
        key can usefully do, and holding a stopwatch to zero it is what the
        physical ones do, so a stopwatch key with no long press of its own is
        given a reset. It is not written to the config, so the key goes back
        to having no long press if its action changes, and a long press
        written by hand wins over it.

        Giving a key a long press changes when its plain press fires: on the
        release rather than on the way down, as it does for any key with a
        second action. For a stopwatch that is the press to start arriving
        when the key comes up, which is the price of holding it to reset.
        </remarks>
        """
        at = (self.page.name, row, column)
        plain, long_press, double = key.actions()
        if (long_press is None and plain is not None
                and plain.type == "stopwatch" and not plain.params.get("reset")):
            long_press = Action("stopwatch", {"reset": True})
        return tuple(Action(action.type, {**action.params, "at": at})
                     if action is not None and action.type in POSITIONAL_ACTIONS else action
                     for action in (plain, long_press, double))

    def _run_actions(self, actions: list) -> None:
        """<summary>
        Run whatever the router decided the gesture meant.
        </summary>
        <param name="actions">Nothing, or one or more actions to run in
        order.</param>
        <remarks>
        The positional actions, toggle, timer, stopwatch and counter, are run
        here rather than by the runner, since only the controller has the
        state they act on; their inner actions go to the runner as usual.

        The keys are redrawn once at the end rather than once per action. A
        hold redraws the whole page, as it always has; anything else whose
        result shows on a key, a state that moved or a volume that changed,
        redraws only the keys whose face changed, after asking the sound
        system afresh.
        </remarks>
        """
        if not actions:
            return
        for action in actions:
            if action.type in POSITIONAL_ACTIONS:
                self._run_positional(action)
            else:
                self.runner.run(action)
        if any(action.type == "hold" for action in actions):
            self.render_keys()  # show or clear the holding mark
        elif any(action.type in POSITIONAL_ACTIONS or action.type in ("volume", "audio_output")
                 for action in actions):
            self._audio_polled = None
            self._refresh_marks()

    def _run_positional(self, action: Action) -> None:
        """<summary>
        Run a toggle, timer, stopwatch or counter against its own key.
        </summary>
        <param name="action">The action, carrying ``at`` from <see cref="_bind"/>.</param>
        <remarks>
        A toggle flips and runs the half for its new state; a timer or
        stopwatch starts, pauses or resumes, or resets when the action says
        so; a counter moves by its step or resets. An action that somehow
        arrives without a position is logged and dropped rather than applied
        to a guessed key.
        </remarks>
        """
        at = action.params.get("at")
        if at is None:
            self.log(f"{action.type}: no key position, nothing done")
            return
        now = self.monotonic()
        params = action.params
        if action.type == "toggle":
            inner = params.get("on" if self.states.toggle(at) else "off")
            if inner is not None:
                self.runner.run(inner)
        elif action.type in ("timer", "stopwatch"):
            if params.get("reset"):
                state = self.states.peek(at)
                if isinstance(state, live.ClockState):
                    state.reset()
            else:
                total = params.get("seconds") if action.type == "timer" else None
                self.states.clock(at, total).press(now)
        elif action.type == "counter":
            self.states.count(at, params.get("step", 0), params.get("reset", False))

    def _timer_done(self, at) -> None:
        """<summary>
        Run the done action of the timer that just finished at a position.
        </summary>
        <param name="at">Page name, row and column of the key.</param>
        <remarks>
        The action is looked up in the config at that moment rather than
        kept with the state, so a done action edited while the timer ran is
        the one that fires. The key is flashed too, whether or not it is on
        the page showing, which is harmless off page: the flash restores
        whatever tile the position holds.
        </remarks>
        """
        name, row, column = at
        for page in self.config.pages:
            if page.name != name:
                continue
            key = page.keys.get((row, column))
            if key is None:
                return
            for action in key.actions():
                if action is not None and action.type == "timer" and action.params.get("done") is not None:
                    self.runner.run(action.params["done"])
                    break
            if page is self.page and self.config.deck.press_flash:
                self._flash(row, column)
            return

    def test_press(self, row: int, column: int, gesture: str = "press") -> None:
        """<summary>
        Act as if a key was pressed, on behalf of the configuration page.
        </summary>
        <param name="row">Row from 0 at the top.</param>
        <param name="column">Column from 0 at the left.</param>
        <param name="gesture">"press", "long" or "double": which of the key's
        three actions to run.</param>
        <remarks>
        The router is deliberately not involved. A request arriving over HTTP
        has no press and release of its own to time, so the page says which
        gesture it wants to try and that action is run outright. Nothing else
        differs: the same runner, the same flash, the same holding mark.

        It draws, so it belongs to the loop thread. The server reaches it
        through <see cref="submit"/> and never calls it directly.
        </remarks>
        """
        key = self.page.keys.get((row, column))
        if key is None:
            return
        bound = self._bind(key, row, column)
        action = {"press": bound[0], "long": bound[1], "double": bound[2]}.get(gesture)
        if action is None:
            return
        if self.config.deck.press_flash:
            self._flash(row, column)
        self._run_actions([action])

    def _flash(self, row: int, column: int) -> None:
        """<summary>
        Brighten the key for a moment so a press is visibly acknowledged.
        </summary>
        <param name="row">Row from 0 at the top.</param>
        <param name="column">Column from 0 at the left.</param>
        <remarks>
        Brightens the picture the panel already has rather than drawing the
        key again, so it costs one send and shows whatever is really on the
        key, animation frame included. It commits on its own, because the
        whole point is that it appears at the moment of the press.

        The panel is put back by <see cref="_end_flashes"/> once the time is
        up. Nothing needs the key to be redrawn in between.
        </remarks>"""
        tile = self.tile_image(row, column)
        if tile is None:
            return
        self.deck.set_position_image(row, column, tiles.flash_tile(tile))
        self.deck.commit()
        self._flashes[(row, column)] = self.monotonic() + FLASH_SECONDS

    def _end_flashes(self) -> bool:
        """<summary>
        Put back every panel whose press flash has run its time.
        </summary>
        <returns>True if anything was queued, so the caller knows a commit is
        needed.</returns>
        <remarks>
        An animated panel is handed back to the animator rather than redrawn
        here, because the animator owns what is on it and would overwrite
        this anyway. A still panel gets its remembered picture again.
        Queues only: the tick commits for the whole pass.
        </remarks>"""
        now = self.monotonic()
        done = [p for p, until in self._flashes.items() if now >= until]
        for row, column in done:
            del self._flashes[(row, column)]
            if (row, column) in self.animator.tracks:
                self.animator.restart(row, column)  # the next frame redraws it
            else:
                tile = self.tile_image(row, column)
                if tile is not None:
                    self.deck.set_position_image(row, column, tile)
        return bool(done)

    def tick(self) -> None:
        """<summary>
        Housekeeping between events: presses, clock, weather, sleep,
        keepalive, config.
        </summary>
        <remarks>
        Runs once each time round the loop, however the loop got there, so
        everything in it has to be cheap when there is nothing to do and has
        to decide for itself whether it is due.

        The order matters in two places. Queued commands run first, so a
        request from the page is applied before anything is drawn from the
        state it changed. The keepalive and the config poll sit outside the
        sleeping check, so a sleeping deck still keeps its handshake going
        and still picks up an edited config.

        The ending flashes and the animation frames share one commit at the
        end rather than committing each in turn, so a pass that changes
        several panels shows them together.
        </remarks>"""
        self._run_commands()
        now = self.monotonic()
        if self.router.pending:
            self._run_actions(self.router.tick(now))
        if self.config.deck.follow_focus:
            self._follow_focus()
        if not self.asleep:
            self.weather.refresh_if_due()
            self.render_strip()
            sent = self._end_flashes()
            if self.animator.tick():
                sent = True
            if sent:
                self.deck.commit()
            # A timer that has run out fires its done action once, then its
            # key shows the finish until the state settles back to idle.
            for at in self.states.settle(now):
                self._timer_done(at)
            # Keeps a blinking repeat border going, clears any border the
            # moment its task stops, and moves the digits on a live key.
            self._refresh_marks()
            # A timer or stopwatch going on the page counts as activity: the
            # deck was asked to show it, and sleeping would hide the count.
            idle_limit = self.config.deck.sleep_after_minutes * 60
            if idle_limit and now - self.last_activity >= idle_limit \
                    and not self.states.any_running(self.page.name):
                self.sleep_deck()
        if now - self.last_keepalive >= KEEPALIVE_SECONDS:
            self.deck.keepalive()
            self.last_keepalive = now
        if self.watch_config and now - self.last_config_poll >= CONFIG_POLL_SECONDS:
            self.last_config_poll = now
            self._reload_if_changed()

    # Per application pages

    def _follow_focus(self) -> None:
        """<summary>
        Switch to the page that matches the focused window, if it has changed.
        </summary>
        <remarks>
        Only ever acts when the focused window is different from the one that
        was acted on last. That is what lets a page switched by hand stay put:
        the deck follows the focus when the focus moves, and otherwise leaves
        whatever the user chose alone.

        When nothing matches, the deck falls back to the first page with no
        match_window of its own, which is the page a config without any of this
        would have shown. A config where every page matches something has no
        fallback, so the current page simply stays.
        </remarks>
        """
        window = self.watcher.current
        signature = window.signature if window is not None else None
        if signature == self._focus_signature:
            return
        self._focus_signature = signature
        target = self._page_for_window(window)
        if target is None or target == self.page.name:
            return
        self.switch_page(target)
        self.log(f"following {window} to page '{target}'")

    def _page_for_window(self, window) -> str | None:
        """<summary>
        Which page belongs to a focused window.
        </summary>
        <param name="window">The focused window, or None when it is unknown.</param>
        <returns>The name of the page to show for that window, or None to stay put.</returns>
        <remarks>
        First match wins, in config order, so a broad pattern placed above a
        narrow one takes both. A page with no match_window of its own is the
        fallback, and the first such page is used; a config where every page
        matches something has no fallback and the current page stays.
        </remarks>"""
        config = self.config
        for page in config.pages:
            if page.match_window and focus.matches(page.match_window, window):
                return page.name
        for page in config.pages:
            if not page.match_window:
                return page.name
        return None

    # Actions on the deck itself (the ActionContext protocol)

    def switch_page(self, target: str) -> None:
        """<summary>
        Show another page, by name or by stepping through them.
        </summary>
        <param name="target">A page name, or "next" or "previous". Stepping
        wraps round at both ends. An unknown name is logged and nothing
        changes, because it usually means a typo in the config rather than
        anything worth stopping for.</param>
        <remarks>
        Wakes a sleeping deck first: a page change means someone is there.
        Any half finished gesture is dropped, so a long press begun on the
        old page cannot fire on whatever key now sits in that position.
        </remarks>"""
        if self.asleep:
            self.wake()  # a request from the page means someone is there
        count = len(self.config.pages)
        if target == "next":
            self.page_index = (self.page_index + 1) % count
        elif target == "previous":
            self.page_index = (self.page_index - 1) % count
        else:
            for index, page in enumerate(self.config.pages):
                if page.name == target:
                    self.page_index = index
                    break
            else:
                self.log(f"page '{target}' not found")
                return
        # A gesture half finished on the old page must not run on the new one.
        self.router.clear()
        self.render_keys()
        self.hub.publish({"type": "page", "page": self.page.name, "page_index": self.page_index})
        self.log(f"page '{self.page.name}'")

    def set_brightness(self, value: int | None = None, delta: int | None = None) -> None:
        """<summary>
        Set the backlight outright, or move it by a step.
        </summary>
        <param name="value">The level to set, 0 to 100.</param>
        <param name="delta">How far to move from where it is, positive or
        negative. Ignored when ``value`` is given.</param>
        <remarks>
        Clamped to 0 to 100 rather than refused, so a key bound to a step
        can be pressed at either end without doing anything surprising.
        Wakes a sleeping deck first, since changing the brightness of a deck
        nobody can see is not what was meant.

        0 is off but still awake. Sleeping is <see cref="sleep_deck"/>.
        </remarks>"""
        if self.asleep:
            self.wake()
        if value is not None:
            self.brightness = value
        elif delta is not None:
            self.brightness += delta
        self.brightness = max(0, min(100, self.brightness))
        self.deck.set_brightness(self.brightness)
        self.hub.publish({"type": "brightness", "value": self.brightness})

    def sleep_deck(self) -> None:
        """<summary>
        Put the screens to sleep. Keys still report presses.
        </summary>
        <remarks>
        Reached either from the idle timer or from the page. Any half
        finished gesture and any running flash are dropped first, so nothing
        is left waiting to fire or to be put back on a screen that is now
        dark.

        Nothing is redrawn while asleep, which is most of the saving. The
        first press wakes the deck and is not passed on to the key.
        </remarks>"""
        if self.asleep:
            return
        self.router.clear()
        self._flashes.clear()
        self.deck.sleep()
        self.asleep = True
        self.hub.publish({"type": "sleep", "asleep": True})
        self.log("deck asleep")

    def wake(self) -> None:
        """<summary>
        Bring a sleeping deck back: initialise, blank, redraw everything.
        </summary>
        <remarks>
        The deck has no wake command, so waking it means initialising it
        again and drawing the whole page afresh. The strip signatures and
        the animations are thrown away first, because they describe what was
        on screens that are now blank and would otherwise stop the page
        being drawn again.

        Any wake counts as activity. Without that, a wake from the window (a
        page change, a brightness change, the Wake button) left the idle clock
        where it was, so the very next tick saw the deck idle past its limit
        and slept it again a fraction of a second later. A key press already
        resets the clock before it calls this.
        </remarks>
        """
        self.asleep = False
        self.last_activity = self.monotonic()
        self.router.clear()
        self.deck.initialise()
        self.deck.set_brightness(self.brightness)
        self._blank_screen()
        self._strip_signatures = [None] * layout.ROWS
        self.animator.clear_all()
        self.render_all()
        self.hub.publish({"type": "sleep", "asleep": False})
        self.log("deck awake")

    # Config reload

    def stop(self) -> None:
        """<summary>
        Let go of any held keys and stop watching the focus. Called when the
        loop ends.
        </summary>
        <remarks>
        The important half is releasing the holds. A hold action is a key
        pressed down somewhere else on the machine, and a daemon that exited
        without letting go would leave it down with nothing left to release
        it.

        Run from a finally in <see cref="run"/>, so it happens on a clean
        stop, on an unplug and on a Ctrl+C alike. Safe to call twice, and it
        leaves the last page on the screens.
        </remarks>"""
        self.runner.holds.stop_all()
        self.router.clear()
        self.watcher.stop()

    def reload(self, config: Config) -> None:
        """<summary>
        Swap in a new config and redraw everything from it.
        </summary>
        <param name="config">An already loaded config. A file that will not
        parse must never reach here: the callers keep the old config in that
        case.</param>
        <remarks>
        Holds are released before anything else, because the new config may
        not have the key that is holding something down and nothing would
        then let it go.

        The page being shown is kept by name, not by number, so editing the
        config leaves the deck where it was as long as that page still
        exists. If it has gone, the first page is shown.

        Everything remembered about what is on the screens, the strip
        signatures, the animations and the flashes, is cleared, since it all
        describes the old config. The focus signature is cleared too, so the
        focused window is matched again against the new pages straight away
        rather than only when it next changes.
        </remarks>"""
        self.runner.holds.stop_all()
        current_name = self.page.name
        new_index = 0
        for index, page in enumerate(config.pages):
            if page.name == current_name:
                new_index = index
                break
        # Assign the index before the config: snapshot() on the server thread
        # reads both, and an old index into a shorter page list would be out of range.
        self.page_index = min(new_index, len(self.config.pages) - 1)
        self.config = config
        self.page_index = new_index
        self.weather.settings = config.weather
        self.router.clear()
        self.router.long_ms = config.deck.long_press_ms
        self.router.double_ms = config.deck.double_press_ms
        self._focus_signature = None  # re-apply the focus against the new pages
        if config.deck.follow_focus:
            self.watcher.poll_seconds = max(0.05, config.deck.focus_poll_ms / 1000)
            self.watcher.start()
        else:
            self.watcher.stop()
        self.brightness = config.deck.brightness
        self.deck.set_brightness(self.brightness)
        self._strip_signatures = [None] * layout.ROWS
        self.animator.clear_all()
        self._flashes.clear()
        self.render_all()
        self.hub.publish({"type": "config", "page": self.page.name})
        self.log("config reloaded")

    def reload_from_disk(self) -> None:
        """<summary>
        Re-read the config file now, whether or not its timestamp moved.
        </summary>
        <remarks>
        What the page's reload button reaches, and the way to pick up an
        edit that a filesystem with a coarse timestamp would hide from the
        poll. Does nothing when the config did not come from a file.

        A config that will not load is logged and the running one is kept,
        so a half typed file cannot leave the deck without a page. The
        stored timestamp is moved on first either way, so a file that is
        broken is not retried on every poll.
        </remarks>"""
        if self.config.path is None:
            return
        self.config_mtime = self._config_mtime()
        try:
            fresh = load(self.config.path)
        except Exception as err:
            self.log(f"config not reloaded: {err}")
            return
        self.reload(fresh)

    def _config_mtime(self) -> float | None:
        """
        <summary>
        The config file's modification time, or None when there is no file or it cannot be read.
        </summary>
        <returns>A float or None.</returns>
        """
        if self.config.path is None:
            return None
        try:
            return os.stat(self.config.path).st_mtime
        except OSError:
            return None

    def _reload_if_changed(self) -> None:
        """<summary>
        Reload the config only if its timestamp has moved since the last
        look.
        </summary>
        <remarks>
        The timestamp is the whole test, so an edit that keeps it, or a
        change made and undone between two polls, is missed. That is
        accepted: the alternative is reading and parsing the file every two
        seconds for ever. <see cref="reload_from_disk"/> is the way to force
        it.

        An editor that writes the file in stages can be caught mid write.
        The load then fails, the old config is kept, and the next save is
        picked up normally.
        </remarks>"""
        mtime = self._config_mtime()
        if mtime is None or mtime == self.config_mtime:
            return
        self.config_mtime = mtime
        try:
            fresh = load(self.config.path)
        except Exception as err:
            self.log(f"config not reloaded: {err}")
            return
        self.reload(fresh)
