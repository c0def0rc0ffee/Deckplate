"""<summary>
Keep an eye on a few web addresses in the background, so a key can light up
while its service answers.
</summary>
<remarks>
A status key is for the home lab: the storage server, the firewall, the
library managers. Each is asked on a timer whether it answers, and the
controller reads the latest answer when it draws the key, so the key's
active face means "up" and its plain face means "down or not yet asked".
A kodi key that names its own player is watched the same way, with the
key's credentials sent along, so its lamp says whether that player is
there before the press finds out.

The asking happens on one thread of its own, never on the deck's loop,
because a server that has gone away takes the whole timeout to say so and
a key press must not wait behind that. The loop reads results under a lock
and never blocks.

An address is up when it answers with a status below 400, or, when the key
names ``expect``, when that text appears in the body. A connection refused,
a timeout and a 500 are all down; the reason is logged once per change, not
once per poll, so a service that is down for a week costs two lines.

Nothing here touches the deck.
</remarks>
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests
import urllib3

from .actions import read_token

DEFAULT_EVERY_MS = 60_000
TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Watch:
    """<summary>One address to keep asking, and how.</summary>
    <param name="url">The address.</param>
    <param name="every_ms">How often to ask.</param>
    <param name="insecure">True to accept a certificate that does not verify,
    which a firewall's own page nearly always has.</param>
    <param name="expect">Text that must appear in the body, or "" for any
    answer below 400.</param>
    <param name="token_file">A credentials file whose contents go in the
    Authorization header, read on every ask as a press reads it; None sends
    no header. A player that wants a password answers 401 without one, which
    would count as down.</param>"""

    url: str
    every_ms: int = DEFAULT_EVERY_MS
    insecure: bool = False
    expect: str = ""
    token_file: str | None = None


def probe(watch: Watch, timeout: float = TIMEOUT_S) -> bool:
    """<summary>Ask one address once and say whether it is up.</summary>
    <param name="watch">What to ask.</param>
    <param name="timeout">Seconds to wait.</param>
    <returns>True when it answers as the watch expects.</returns>
    <exception cref="requests.RequestException">It could not be reached.</exception>
    <exception cref="OSError">The watch names a credentials file that cannot be read.</exception>"""
    if watch.insecure:
        # The key asked for an unverified certificate, so the warning urllib3
        # prints for one is noise in the daemon's log, not news.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    headers = {"User-Agent": "deckplate"}
    if watch.token_file:
        headers["Authorization"] = read_token(watch.token_file)
    response = requests.get(watch.url, timeout=timeout, verify=not watch.insecure, headers=headers)
    if watch.expect:
        return watch.expect in response.text
    return response.status_code < 400


class StatusMonitor:
    """<summary>
    Polls every watched address on a background thread and remembers the
    last answer for each.
    </summary>
    <remarks>
    Built idle. <see cref="watch"/> replaces the set of addresses, which is
    what the controller calls at start and on every reload, and the thread is
    started on the first non empty set. Removing an address drops its answer,
    so a key that comes back later starts as not yet asked rather than stale.

    The prober and the clock are injectable: the tests hand in a function that
    answers from a table and drive <see cref="poll_once"/> by hand, with no
    thread and no network.
    </remarks>
    """

    def __init__(self, *, prober: Callable[[Watch], bool] = probe,
                 monotonic: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, threaded: bool = True) -> None:
        """<summary>An empty monitor, no thread yet.</summary>
        <param name="prober">Answers whether one watch is up, or raises.</param>
        <param name="monotonic">The clock the schedule runs on.</param>
        <param name="log">Where changes of state go.</param>
        <param name="threaded">False to never start the thread, so a test
        drives <see cref="poll_once"/> by hand.</param>"""
        self.prober = prober
        self.monotonic = monotonic
        self.log = log
        self.threaded = threaded
        self._lock = threading.Lock()
        self._watches: dict[str, Watch] = {}
        self._state: dict[str, bool | None] = {}
        self._due: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake = threading.Event()

    def watch(self, watches: list[Watch]) -> None:
        """<summary>Replace the set of addresses being asked.</summary>
        <param name="watches">The new set. Duplicated addresses keep the first.</param>
        <remarks>Every new address is due at once, so a key gains its state
        within one poll of the config loading rather than a minute later.</remarks>"""
        with self._lock:
            fresh = {}
            for item in watches:
                fresh.setdefault(item.url, item)
            for url in list(self._state):
                if url not in fresh:
                    del self._state[url]
                    self._due.pop(url, None)
            for url in fresh:
                if url not in self._watches:
                    self._due[url] = 0.0
                    self._state[url] = None
            self._watches = fresh
        if fresh and self.threaded:
            self._wake.set()
            self._ensure_thread()

    def ok(self, url: str) -> bool | None:
        """<summary>The last answer for an address.</summary>
        <param name="url">The address as the key wrote it.</param>
        <returns>True for up, False for down, None for not watched or not yet asked.</returns>"""
        with self._lock:
            return self._state.get(url)

    def poll_once(self) -> float:
        """<summary>Ask every address that is due, and say when the next one is.</summary>
        <returns>Seconds until the earliest next ask, or 60 when nothing is watched.</returns>
        <remarks>The prober runs outside the lock, so a slow server never
        stalls <see cref="ok"/>; only the bookkeeping is locked.</remarks>"""
        now = self.monotonic()
        with self._lock:
            due = [item for url, item in self._watches.items() if self._due.get(url, 0.0) <= now]
        for item in due:
            try:
                up = bool(self.prober(item))
                why = ""
            except Exception as err:
                up = False
                why = f" ({err})"
            with self._lock:
                if item.url not in self._watches:
                    continue
                before = self._state.get(item.url)
                self._state[item.url] = up
                self._due[item.url] = self.monotonic() + item.every_ms / 1000
            if before != up:
                self.log(f"status {item.url}: {'up' if up else 'down'}{why}")
        with self._lock:
            if not self._watches:
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
        self._thread = threading.Thread(target=self._run, name="status monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """<summary>The thread body: poll, sleep until the next one is due, repeat.</summary>"""
        while not self._stop_event.is_set():
            self._wake.clear()
            try:
                wait = self.poll_once()
            except Exception as err:  # a bug in the prober must not end the monitor
                self.log(f"status monitor: {err}")
                wait = 30.0
            self._wake.wait(wait)
