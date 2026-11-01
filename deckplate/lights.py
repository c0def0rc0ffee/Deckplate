"""<summary>
WLED lights, as far as a deck key needs them: the state a key draws, the
command a press sends, and a monitor that keeps the state fresh.
</summary>
<remarks>
WLED is the open firmware on an ESP board driving a string of addressable
LEDs. It answers ``GET /json/state`` with whether the lights are on, the
brightness, the preset in use and each segment's colours, and takes the same
shape back on ``POST /json/state`` to change any of it. Asking for ``"v"``
in a post makes WLED answer with the full state instead of a bare success,
which is how a press updates the key without waiting for the next poll.

A ``wled`` key names the device's address and what the press does: toggle,
on, off, a preset by number, or a step of brightness either way. The face is
drawn from the latest state by <see cref="tiles.lights_tile"/>: a lamp,
the brightness as a bar, and the lights' own colour behind it.

Polling runs on the monitor's thread, never on the deck's loop, as the
status monitor does. Nothing here touches the deck.
</remarks>
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

TIMEOUT_S = 4.0
DEFAULT_EVERY_MS = 10_000
STEP_DEFAULT = 32
BRIGHTNESS_MAX = 255
DOINGS = ("toggle", "on", "off", "preset", "brighter", "dimmer")


@dataclass(frozen=True)
class LightState:
    """<summary>What the lights are doing, as a key needs to know it.</summary>
    <param name="on">Whether they are lit.</param>
    <param name="brightness">0 to 255, WLED's own scale.</param>
    <param name="colour">The first colour of the first segment, RGB.</param>
    <param name="preset">The preset in use, or -1 for none.</param>"""

    on: bool
    brightness: int
    colour: tuple[int, int, int]
    preset: int = -1

    @property
    def percent(self) -> int:
        """<summary>The brightness as a whole percentage.</summary>"""
        return round(self.brightness * 100 / BRIGHTNESS_MAX)


def state_url(url: str) -> str:
    """<summary>The state endpoint for a device address.</summary>
    <param name="url">The address as the key wrote it: the device's root, with
    or without a trailing slash, or the state endpoint itself.</param>
    <returns>The ``/json/state`` address.</returns>"""
    base = url.rstrip("/")
    return base if base.endswith("/json/state") else base + "/json/state"


def parse_state(payload: Any) -> LightState:
    """<summary>Read a WLED state reply.</summary>
    <param name="payload">The decoded JSON, from a GET or a verbose POST.</param>
    <returns>The state.</returns>
    <exception cref="ValueError">The reply is not a WLED state.</exception>"""
    if not isinstance(payload, dict) or "on" not in payload:
        raise ValueError("not a WLED state")
    colour = (0, 0, 0)
    try:
        first = payload["seg"][0]["col"][0]
        colour = (int(first[0]), int(first[1]), int(first[2]))
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    try:
        brightness = max(0, min(BRIGHTNESS_MAX, int(payload.get("bri", 0))))
    except (TypeError, ValueError):
        brightness = 0
    try:
        preset = int(payload.get("ps", -1))
    except (TypeError, ValueError):
        preset = -1
    return LightState(bool(payload["on"]), brightness, colour, preset)


def fetch_state(url: str, timeout: float = TIMEOUT_S) -> LightState:
    """<summary>Ask a device for its state.</summary>
    <param name="url">The device address, as the key wrote it.</param>
    <param name="timeout">Seconds to wait.</param>
    <returns>The state.</returns>
    <exception cref="requests.RequestException">It could not be reached.</exception>
    <exception cref="ValueError">It answered with something else.</exception>"""
    response = requests.get(state_url(url), timeout=timeout, headers={"User-Agent": "deckplate"})
    response.raise_for_status()
    return parse_state(response.json())


def command(params: dict, state: LightState | None) -> dict:
    """<summary>The body a wled key's press posts.</summary>
    <param name="params">The action's params: ``do``, and ``preset`` or
    ``step`` as the doing needs.</param>
    <param name="state">The latest known state, for a brightness step, or
    None when none is known yet.</param>
    <returns>The JSON body, always asking for the state back with ``v``.</returns>
    <remarks>A brightness step with no known state steps from the middle,
    which is at worst one press wrong. A step always turns the lights on,
    since a brighter press on dark lights means light them.</remarks>
    <exception cref="ValueError">An unknown doing.</exception>"""
    do = params.get("do", "toggle")
    body: dict[str, Any] = {"v": True}
    if do == "toggle":
        body["on"] = "t"
    elif do == "on":
        body["on"] = True
    elif do == "off":
        body["on"] = False
    elif do == "preset":
        body["on"] = True
        body["ps"] = int(params["preset"])
    elif do in ("brighter", "dimmer"):
        step = int(params.get("step", STEP_DEFAULT))
        current = state.brightness if state is not None else BRIGHTNESS_MAX // 2
        body["on"] = True
        body["bri"] = max(1, min(BRIGHTNESS_MAX, current + (step if do == "brighter" else -step)))
    else:
        raise ValueError(f"unknown lights action '{do}'")
    return body


class LightsMonitor:
    """<summary>
    Polls every watched device on a background thread and remembers the
    last state of each.
    </summary>
    <remarks>
    The lights' twin of the status monitor, holding a state rather than a
    yes or no. <see cref="watch"/> replaces the set of devices at start and
    on every reload; <see cref="update"/> lets a press drop the state WLED
    sent back straight in, so the face follows the press at once.

    The prober and the clock are injectable, and ``threaded=False`` keeps the
    thread from ever starting, so the tests drive <see cref="poll_once"/> by
    hand with no network.
    </remarks>
    """

    def __init__(self, *, prober: Callable[[str], LightState] = fetch_state,
                 monotonic: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, threaded: bool = True) -> None:
        """<summary>An empty monitor, no thread yet.</summary>
        <param name="prober">Gives one device's state from its address, or raises.</param>
        <param name="monotonic">The clock the schedule runs on.</param>
        <param name="log">Where reachability changes go.</param>
        <param name="threaded">False to never start the thread.</param>"""
        self.prober = prober
        self.monotonic = monotonic
        self.log = log
        self.threaded = threaded
        self._lock = threading.Lock()
        self._every: dict[str, int] = {}
        self._state: dict[str, LightState | None] = {}
        self._due: dict[str, float] = {}
        self._asked: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake = threading.Event()

    def watch(self, devices: list[tuple[str, int]]) -> None:
        """<summary>Replace the set of devices being asked.</summary>
        <param name="devices">(address, every_ms) pairs. A repeated address
        keeps the shortest interval.</param>"""
        with self._lock:
            fresh: dict[str, int] = {}
            for url, every_ms in devices:
                fresh[url] = min(every_ms, fresh.get(url, every_ms))
            for url in list(self._state):
                if url not in fresh:
                    del self._state[url]
                    self._due.pop(url, None)
                    self._asked.discard(url)
            for url in fresh:
                if url not in self._every:
                    self._due[url] = 0.0
                    self._state[url] = None
            self._every = fresh
        if fresh and self.threaded:
            self._wake.set()
            self._ensure_thread()

    def state(self, url: str) -> LightState | None:
        """<summary>The last state for a device.</summary>
        <param name="url">The address as the key wrote it.</param>
        <returns>The state, or None when unreachable, not watched or not yet asked.</returns>"""
        with self._lock:
            return self._state.get(url)

    def update(self, url: str, state: LightState) -> None:
        """<summary>Record a state a press got back, and push the next poll out.</summary>
        <param name="url">The device address.</param>
        <param name="state">What WLED answered.</param>"""
        with self._lock:
            if url in self._every:
                self._state[url] = state
                self._due[url] = self.monotonic() + self._every[url] / 1000

    def poll_once(self) -> float:
        """<summary>Ask every device that is due, and say when the next one is.</summary>
        <returns>Seconds until the earliest next ask, or 60 when nothing is watched.</returns>"""
        now = self.monotonic()
        with self._lock:
            due = [url for url in self._every if self._due.get(url, 0.0) <= now]
        for url in due:
            try:
                state: LightState | None = self.prober(url)
                why = ""
            except Exception as err:
                state = None
                why = f" ({err})"
            with self._lock:
                if url not in self._every:
                    continue
                before = self._state.get(url)
                first = url not in self._asked
                self._asked.add(url)
                self._state[url] = state
                self._due[url] = self.monotonic() + self._every[url] / 1000
            if first or (before is None) != (state is None):
                self.log(f"lights {url}: {'reachable' if state is not None else 'unreachable'}{why}")
        with self._lock:
            if not self._every:
                return 60.0
            return max(0.1, min(self._due.values()) - self.monotonic())

    def stop(self) -> None:
        """<summary>End the polling thread, if there is one.</summary>"""
        self._stop_event.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _ensure_thread(self) -> None:
        """<summary>Start the polling thread the first time something is watched.</summary>"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="lights monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """<summary>The thread body: poll, sleep until the next one is due, repeat.</summary>"""
        while not self._stop_event.is_set():
            self._wake.clear()
            try:
                wait = self.poll_once()
            except Exception as err:  # a bug in the prober must not end the monitor
                self.log(f"lights monitor: {err}")
                wait = 30.0
            self._wake.wait(wait)
