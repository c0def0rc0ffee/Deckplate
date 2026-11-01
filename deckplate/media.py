"""<summary>
What the desktop's media players are playing, for the now playing panel.
</summary>
<remarks>
Players announce themselves on the session bus under the MPRIS name, so the
reading is: list the names, take the first player that is playing (or the
first at all), and read its title, artist and state. No player library, no
extra program: the bus is already there.

Shaped like the weather: a frozen reading that compares by value, and a
service the controller pokes each pass of its loop that only does anything
when its interval is up. Two seconds is the interval, so a track change
shows on the strip within a couple of seconds without asking the bus on every
pass.
</remarks>
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

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
