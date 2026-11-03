"""<summary>
What a media player is playing, for the now playing panel: the desktop's
own players over the session bus, or a Kodi player anywhere on the network.
</summary>
<remarks>
Players announce themselves on the session bus under the MPRIS name, so the
local reading is: list the names, take the first player that is playing (or
the first at all), and read its title, artist and state. No player library,
no extra program: the bus is already there.

A panel may instead name a Kodi player, by the same JSON RPC address and
credentials file a kodi key uses. That player is asked on a thread of its
own by <see cref="KodiPlayingMonitor"/>, since a box in another room can
take the whole timeout to answer and the deck's loop must not wait on it.

Shaped like the weather: a frozen reading that compares by value, and a
service the controller pokes each pass of its loop that only does anything
when its interval is up. Two seconds is the interval, so a track change
shows on the strip within a couple of seconds without asking the bus on every
pass.
</remarks>
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import kodi as kodi_module
from . import sessionbus

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
REFRESH_SECONDS = 2.0


@dataclass(frozen=True)
class NowPlaying:
    """<summary>One reading of a media player.</summary>
    <param name="title">The track title, or "" when the player has none.</param>
    <param name="artist">The first artist, or "".</param>
    <param name="status">"Playing", "Paused" or "Stopped".</param>
    <param name="player">The player's short name, from its bus name.</param>"""

    title: str
    artist: str
    status: str
    player: str

    @property
    def playing(self) -> bool:
        """<summary>True while the player says it is playing.</summary>
        <returns>A bool.</returns>"""
        return self.status == "Playing"


def read_player(name: str) -> NowPlaying:
    """<summary>Read one player's state and track from the bus.</summary>
    <param name="name">Its full bus name.</param>
    <returns>The reading.</returns>
    <exception cref="sessionbus.BusError">The player went away or the read failed.</exception>"""
    status = sessionbus.get_property(name, MPRIS_PATH, PLAYER, "PlaybackStatus")
    metadata = sessionbus.get_property(name, MPRIS_PATH, PLAYER, "Metadata") or {}
    title = str(metadata.get("xesam:title") or "")
    artists = metadata.get("xesam:artist") or []
    if isinstance(artists, str):
        artists = [artists]
    artist = str(artists[0]) if artists else ""
    return NowPlaying(title=title, artist=artist, status=str(status), player=name[len(MPRIS_PREFIX):])


def fetch() -> NowPlaying | None:
    """<summary>The player worth showing, or None when nothing is open.</summary>
    <returns>The playing player if there is one, else the first player, else None.</returns>
    <remarks>A player that fails to answer is skipped rather than failing
    the whole reading, since one that is quitting often does.</remarks>
    <exception cref="sessionbus.BusError">The bus itself is unavailable.</exception>"""
    names = [name for name in sessionbus.list_names() if name.startswith(MPRIS_PREFIX)]
    first: NowPlaying | None = None
    for name in names:
        try:
            reading = read_player(name)
        except sessionbus.BusError:
            continue
        if reading.playing:
            return reading
        first = first or reading
    return first


class MediaService:
    """<summary>Holds the latest reading and refreshes it on a short timer.</summary>
    <remarks>Driven by the controller's loop like the weather service. A
    failed read keeps the last reading for one interval, then clears it, so
    a bus that goes away leaves the panel showing nothing rather than a stale
    track for ever.</remarks>"""

    def __init__(self, *, fetcher: Callable[[], NowPlaying | None] = fetch,
                 monotonic: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, interval: float = REFRESH_SECONDS) -> None:
        """<summary>Build the service. Nothing is read here.</summary>
        <param name="fetcher">How to read the players. Swapped in the tests.</param>
        <param name="monotonic">The clock.</param>
        <param name="log">Where a failure goes, once per change of state.</param>
        <param name="interval">Seconds between reads.</param>"""
        self.fetcher = fetcher
        self.monotonic = monotonic
        self.log = log
        self.interval = interval
        self.reading: NowPlaying | None = None
        self._next_due = 0.0
        self._failed = False

    def refresh_if_due(self) -> bool:
        """<summary>Read again when the interval has passed.</summary>
        <returns>True when the reading changed.</returns>"""
        if self.monotonic() < self._next_due:
            return False
        self._next_due = self.monotonic() + self.interval
        try:
            fresh = self.fetcher()
            if self._failed:
                self._failed = False
        except Exception as err:
            if not self._failed:
                self.log(f"now playing unavailable: {err}")
                self._failed = True
            fresh = None
        changed = fresh != self.reading
        self.reading = fresh
        return changed


# ---- A Kodi player as the source -------------------------------------------

KODI_EVERY_MS = 3000
KODI_ITEM_PROPERTIES = ["title", "artist", "showtitle", "season", "episode"]


def kodi_reading(players: Any, item: Any, properties: Any) -> NowPlaying | None:
    """<summary>
    One reading of a Kodi player from the three replies that describe it.
    </summary>
    <param name="players">The Player.GetActivePlayers reply.</param>
    <param name="item">The Player.GetItem reply for the first player.</param>
    <param name="properties">The Player.GetProperties reply asking for speed.</param>
    <returns>The reading, or None when no player is active.</returns>
    <remarks>
    Pure, so the tests can hand in replies as dicts. The title is the item's
    own title, else Kodi's label for it, which is what a file with no tags
    carries. The second line is the artist for music and the show, with the
    season and episode when both are known, for television; a film has
    neither and gets no second line. Kodi reports a paused player as speed
    zero, which is the whole of the play or pause reading.
    </remarks>
    """
    if kodi_module.first_player(players) is None:
        return None
    try:
        data = item["result"]["item"]
    except (KeyError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    title = str(data.get("title") or data.get("label") or "")
    artists = data.get("artist") or []
    if isinstance(artists, str):
        artists = [artists]
    artist = str(artists[0]) if artists else ""
    if not artist and data.get("showtitle"):
        artist = str(data["showtitle"])
        season, episode = data.get("season"), data.get("episode")
        if isinstance(season, int) and isinstance(episode, int) and season >= 0 and episode >= 0:
            artist += f" {season}x{episode:02d}"
    try:
        speed = properties["result"]["speed"]
    except (KeyError, TypeError):
        speed = 1
    status = "Paused" if speed in (0, 0.0) else "Playing"
    return NowPlaying(title=title, artist=artist, status=status, player="kodi")


def fetch_kodi(url: str, token_file: str | None, send_json: Callable[[str, dict, dict, float], dict],
               read_token: Callable[[str], str], timeout: float) -> NowPlaying | None:
    """<summary>Ask a Kodi player what it is playing.</summary>
    <param name="url">Its JSON RPC address.</param>
    <param name="token_file">The credentials file whose contents go in the
    Authorization header, or None for no header.</param>
    <param name="send_json">How to post a JSON RPC body and read the reply.</param>
    <param name="read_token">How to read the credentials file.</param>
    <param name="timeout">Seconds to wait for each answer.</param>
    <returns>The reading, or None when nothing is playing.</returns>
    <remarks>Three requests: the active players, then the item and the speed
    of the first. A player with nothing on answers the first with an empty
    list and the other two are not sent.</remarks>
    <exception cref="Exception">The player could not be reached or the
    credentials file could not be read; the monitor turns either into an
    empty reading and one log line.</exception>"""
    headers: dict[str, str] = {}
    if token_file:
        headers["Authorization"] = read_token(token_file)
    players = send_json(url, headers, kodi_module.payload("Player.GetActivePlayers"), timeout)
    player = kodi_module.first_player(players)
    if player is None:
        return None
    item = send_json(url, headers, kodi_module.payload(
        "Player.GetItem", {"playerid": player, "properties": KODI_ITEM_PROPERTIES}), timeout)
    properties = send_json(url, headers, kodi_module.payload(
        "Player.GetProperties", {"playerid": player, "properties": ["speed"]}), timeout)
    return kodi_reading(players, item, properties)


def default_kodi_prober(url: str, token_file: str | None) -> NowPlaying | None:
    """<summary>The monitor's prober outside the tests: the real request and the real credentials file.</summary>
    <param name="url">The player's JSON RPC address.</param>
    <param name="token_file">Its credentials file, or None.</param>
    <returns>The reading, or None when nothing is playing.</returns>
    <remarks>The actions module is imported here rather than at the top
    because it is the heavier side of the daemon and this file is read by
    the tests and the tile renderer, neither of which should pull it in.</remarks>"""
    from .actions import default_json_request, read_token
    return fetch_kodi(url, token_file, default_json_request, read_token, KODI_TIMEOUT_S)


KODI_TIMEOUT_S = 5.0


class KodiPlayingMonitor:
    """<summary>
    Polls every Kodi player a strip panel shows on a background thread and
    remembers the last reading for each.
    </summary>
    <remarks>
    The local media reader asks the session bus from the controller's own
    loop, because the bus answers at once. A player in another room does
    not: one that has gone away takes the whole timeout to say so, and the
    loop that draws the keys must not wait behind it. So this is the lights
    monitor's shape rather than the media service's: one thread of its own,
    readings taken under a lock, and the controller reading whatever is
    latest when it draws.

    An unreachable player reads as nothing playing, and the change is logged
    once each way rather than once per poll.

    The prober and the clock are injectable, and ``threaded=False`` keeps the
    thread from ever starting, so the tests drive <see cref="poll_once"/> by
    hand with no network.
    </remarks>
    """

    def __init__(self, *, prober: Callable[[str, str | None], NowPlaying | None] = default_kodi_prober,
                 monotonic: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, threaded: bool = True,
                 every_ms: int = KODI_EVERY_MS) -> None:
        """<summary>An empty monitor, no thread yet.</summary>
        <param name="prober">Reads one player from its address and credentials file, or raises.</param>
        <param name="monotonic">The clock the schedule runs on.</param>
        <param name="log">Where reachability changes go.</param>
        <param name="threaded">False to never start the thread.</param>
        <param name="every_ms">How often each player is asked.</param>"""
        self.prober = prober
        self.monotonic = monotonic
        self.log = log
        self.threaded = threaded
        self.every_ms = every_ms
        self._lock = threading.Lock()
        self._players: dict[str, str | None] = {}
        self._reading: dict[str, NowPlaying | None] = {}
        self._due: dict[str, float] = {}
        self._down: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake = threading.Event()

    def watch(self, players: list[tuple[str, str | None]]) -> None:
        """<summary>Replace the set of players being asked.</summary>
        <param name="players">(address, credentials file or None) pairs. A
        repeated address keeps the first.</param>
        <remarks>Every new address is due at once, so a panel shows the
        track within one poll of the config loading.</remarks>"""
        with self._lock:
            fresh: dict[str, str | None] = {}
            for url, token_file in players:
                fresh.setdefault(url, token_file)
            for url in list(self._reading):
                if url not in fresh:
                    del self._reading[url]
                    self._due.pop(url, None)
                    self._down.discard(url)
            for url in fresh:
                if url not in self._players:
                    self._due[url] = 0.0
                    self._reading[url] = None
            self._players = fresh
        if fresh and self.threaded:
            self._wake.set()
            self._ensure_thread()

    def reading(self, url: str) -> NowPlaying | None:
        """<summary>The last reading for a player.</summary>
        <param name="url">The address as the panel wrote it.</param>
        <returns>The reading, or None when nothing is playing, the player is
        unreachable, not watched or not yet asked.</returns>"""
        with self._lock:
            return self._reading.get(url)

    def poll_once(self) -> float:
        """<summary>Ask every player that is due, and say when the next one is.</summary>
        <returns>Seconds until the earliest next ask, or 60 when nothing is watched.</returns>
        <remarks>The prober runs outside the lock, so a slow player never
        stalls <see cref="reading"/>; only the bookkeeping is locked.</remarks>"""
        now = self.monotonic()
        with self._lock:
            due = [(url, token_file) for url, token_file in self._players.items() if self._due.get(url, 0.0) <= now]
        for url, token_file in due:
            try:
                fresh = self.prober(url, token_file)
                why = ""
            except Exception as err:
                fresh = None
                why = str(err)
            with self._lock:
                if url not in self._players:
                    continue
                self._reading[url] = fresh
                self._due[url] = self.monotonic() + self.every_ms / 1000
                was_down = url in self._down
                if why:
                    self._down.add(url)
                else:
                    self._down.discard(url)
            if why and not was_down:
                self.log(f"now playing {url}: unreachable ({why})")
            elif was_down and not why:
                self.log(f"now playing {url}: reachable")
        with self._lock:
            if not self._players:
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
        self._thread = threading.Thread(target=self._run, name="kodi playing monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """<summary>The thread body: poll, sleep until the next one is due, repeat.</summary>"""
        while not self._stop_event.is_set():
            self._wake.clear()
            try:
                wait = self.poll_once()
            except Exception as err:  # a bug in the prober must not end the monitor
                self.log(f"kodi playing monitor: {err}")
                wait = 30.0
            self._wake.wait(wait)
