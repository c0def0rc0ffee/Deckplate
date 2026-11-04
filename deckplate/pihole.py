"""<summary>
Pi-hole, as far as a deck key needs it: the reading a key draws, the sign
in its API wants, the pause a press sends, and a monitor that keeps the
reading fresh.
</summary>
<remarks>
Pi-hole 6 answers its own REST API at ``/api``. Nearly every call needs a
session: ``POST /api/auth`` with the web password, or an app password made
on the settings page, answers with a session id that then goes in the
``sid`` header, and the session lapses after a few minutes of quiet. The
reading comes from ``/api/stats/summary``, today's queries and how many
were blocked, and ``/api/dns/blocking``, whether blocking is on and how
long a pause has left. ``POST /api/dns/blocking`` pauses or resumes it.

A ``pihole`` key names the box's address and, usually, a credentials file
holding the password; the face is drawn from the latest reading by
<see cref="tiles.pihole_tile"/> and the lamp in the corner says whether
blocking is on. The press pauses blocking for a while and resumes it, or
opens the admin page.

One session per box is kept here and renewed when the box says it has
lapsed, so a press and the monitor share it rather than each signing in.
Polling runs on the monitor's thread, never on the deck's loop. Nothing
here touches the deck, and the password is read from its file on every
sign in and kept nowhere else.
</remarks>
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import requests
import urllib3

TIMEOUT_S = 4.0
DEFAULT_EVERY_MS = 10_000
DEFAULT_PAUSE_S = 300
PAUSE_RANGE = (10, 86_400)
DOINGS = ("toggle", "open")


@dataclass(frozen=True)
class PiholeReading:
    """<summary>Pi-hole today, as a key needs to know it.</summary>
    <param name="blocking">Whether blocking is on.</param>
    <param name="timer">Seconds left of a pause, or None when none is running.</param>
    <param name="queries">Queries answered today.</param>
    <param name="blocked">Of those, how many were blocked.</param>
    <param name="percent">The blocked share of today's queries, 0 to 100.</param>
    <param name="domains">Domains on the block lists.</param>"""

    blocking: bool
    timer: int | None
    queries: int
    blocked: int
    percent: float
    domains: int


def api_url(url: str, path: str) -> str:
    """<summary>An API address under a box's root.</summary>
    <param name="url">The address as the key wrote it: the box's root, with
    or without a trailing slash or ``/admin``.</param>
    <param name="path">The call, such as ``/stats/summary``.</param>
    <returns>The full address.</returns>"""
    base = url.rstrip("/")
    if base.endswith("/admin"):
        base = base[:-len("/admin")]
    return base + "/api" + path


def admin_url(url: str) -> str:
    """<summary>The admin page for a box, for a key that opens it.</summary>
    <param name="url">The address as the key wrote it.</param>
    <returns>The ``/admin/`` address.</returns>"""
    base = url.rstrip("/")
    return base if base.endswith("/admin") else base + "/admin/"


def read_password(path: str) -> str:
    """<summary>The password held in a credentials file, as it stands.</summary>
    <param name="path">The file, as written in the config. A ``~`` is expanded.</param>
    <returns>The file's text with surrounding whitespace stripped.</returns>
    <remarks>Unlike <see cref="actions.read_token"/> no scheme is put in
    front: Pi-hole takes the bare password in a JSON body, not a header.
    Read on every sign in, so a changed password is picked up without a
    restart and never sits in a Config object.</remarks>
    <exception cref="OSError">The file cannot be read.</exception>"""
    return Path(path).expanduser().read_text(encoding="utf-8").strip()


def parse_blocking(payload: Any) -> tuple[bool, int | None]:
    """<summary>Read a ``/dns/blocking`` reply.</summary>
    <param name="payload">The decoded JSON.</param>
    <returns>(blocking, seconds left of a pause or None).</returns>
    <exception cref="ValueError">The reply is not a blocking state.</exception>"""
    if not isinstance(payload, dict) or payload.get("blocking") not in ("enabled", "disabled"):
        raise ValueError("not a Pi-hole blocking state")
    timer = payload.get("timer")
    try:
        timer = int(round(float(timer))) if timer is not None else None
    except (TypeError, ValueError):
        timer = None
    return payload["blocking"] == "enabled", timer


def parse_summary(summary: Any, blocking: Any) -> PiholeReading:
    """<summary>Build a reading from the summary and blocking replies.</summary>
    <param name="summary">The decoded ``/stats/summary`` reply.</param>
    <param name="blocking">The decoded ``/dns/blocking`` reply.</param>
    <returns>The reading.</returns>
    <exception cref="ValueError">Either reply is not what Pi-hole sends.</exception>"""
    if not isinstance(summary, dict) or not isinstance(summary.get("queries"), dict):
        raise ValueError("not a Pi-hole summary")
    queries = summary["queries"]
    on, timer = parse_blocking(blocking)

    def number(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    total = number(queries.get("total"))
    blocked = number(queries.get("blocked"))
    try:
        percent = float(queries.get("percent_blocked"))
    except (TypeError, ValueError):
        percent = blocked * 100 / total if total else 0.0
    gravity = summary.get("gravity") if isinstance(summary.get("gravity"), dict) else {}
    return PiholeReading(on, timer, total, blocked, max(0.0, min(100.0, percent)), number(gravity.get("domains_being_blocked")))


def default_request(method: str, url: str, headers: dict, body: dict | None, timeout: float, verify: bool) -> tuple[int, Any]:
    """<summary>One HTTP call, answered as a status and the decoded JSON.</summary>
    <param name="method">GET, POST or DELETE.</param>
    <param name="url">The address.</param>
    <param name="headers">Headers, the session id among them once there is one.</param>
    <param name="body">A JSON body, or None.</param>
    <param name="timeout">Seconds to wait.</param>
    <param name="verify">False to accept a certificate that does not verify.</param>
    <returns>(status code, decoded body or None).</returns>
    <remarks>A 401 is returned, not raised, because it is how Pi-hole says a
    session has lapsed and the caller signs in again on it.</remarks>
    <exception cref="requests.RequestException">The box could not be reached.</exception>"""
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    sent = {"User-Agent": "deckplate", **headers}
    response = requests.request(method, url, headers=sent, json=body, timeout=timeout, verify=verify)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code not in (200, 401):
        response.raise_for_status()
    return response.status_code, payload


class PiholeSession:
    """<summary>
    One box's session: signs in when it has to and puts the session id on
    every call.
    </summary>
    <remarks>
    The session id is kept here and nowhere else; the password is read from
    its file each time a sign in is needed. A call answered 401 signs in
    once and goes again, which is what a lapsed session looks like, and a
    box with no password set answers calls without any session at all.
    Shared by the monitor's thread and a press on the deck's loop, so the
    sign in is under a lock.
    </remarks>
    """

    def __init__(self, url: str, token_file: str | None, insecure: bool = False,
                 request: Callable[..., tuple[int, Any]] = default_request,
                 read_password: Callable[[str], str] = read_password) -> None:
        """<summary>A session for one box, not yet signed in.</summary>
        <param name="url">The box's address as the key wrote it.</param>
        <param name="token_file">The credentials file holding the password, or None.</param>
        <param name="insecure">True to accept a certificate that does not verify.</param>
        <param name="request">How calls are made. Swapped in the tests.</param>
        <param name="read_password">How the password is read.</param>"""
        self.url = url
        self.token_file = token_file
        self.insecure = insecure
        self.request = request
        self.read_password = read_password
        self.sid: str | None = None
        self._lock = threading.Lock()

    def _login(self) -> None:
        """<summary>Sign in and keep the session id.</summary>
        <exception cref="ValueError">The box refused the password.</exception>"""
        if not self.token_file:
            raise ValueError("the box wants a password and the key names no credentials file")
        password = self.read_password(self.token_file)
        status, payload = self.request("POST", api_url(self.url, "/auth"), {}, {"password": password}, TIMEOUT_S, not self.insecure)
        session = payload.get("session") if isinstance(payload, dict) else None
        if status != 200 or not isinstance(session, dict) or not session.get("valid") or not session.get("sid"):
            raise ValueError("Pi-hole refused the password")
        self.sid = str(session["sid"])

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        """<summary>Make one API call, signing in first or again as needed.</summary>
        <param name="method">GET, POST or DELETE.</param>
        <param name="path">The call, such as ``/stats/summary``.</param>
        <param name="body">A JSON body, or None.</param>
        <returns>The decoded reply.</returns>
        <exception cref="ValueError">The box refused the password, or still
        answered 401 after a fresh sign in.</exception>
        <exception cref="requests.RequestException">The box could not be reached.</exception>"""
        with self._lock:
            for attempt in (1, 2):
                headers = {"sid": self.sid} if self.sid else {}
                status, payload = self.request(method, api_url(self.url, path), headers, body, TIMEOUT_S, not self.insecure)
                if status == 200:
                    return payload
                self.sid = None
                if attempt == 1:
                    self._login()
            raise ValueError("Pi-hole refused the call after signing in")


def fetch(session: PiholeSession) -> PiholeReading:
    """<summary>Ask a box for today's figures and the blocking state.</summary>
    <param name="session">The box's session.</param>
    <returns>The reading.</returns>
    <exception cref="ValueError">A reply was not what Pi-hole sends, or the password was refused.</exception>
    <exception cref="requests.RequestException">The box could not be reached.</exception>"""
    summary = session.call("GET", "/stats/summary")
    blocking = session.call("GET", "/dns/blocking")
    return parse_summary(summary, blocking)


def set_blocking(session: PiholeSession, blocking: bool, seconds: int | None = None) -> tuple[bool, int | None]:
    """<summary>Pause or resume blocking.</summary>
    <param name="session">The box's session.</param>
    <param name="blocking">False to pause, True to resume.</param>
    <param name="seconds">How long a pause lasts; None for until resumed.
    Ignored when resuming.</param>
    <returns>(blocking, seconds left), as the box answers.</returns>
    <exception cref="ValueError">The reply was not a blocking state, or the password was refused.</exception>
    <exception cref="requests.RequestException">The box could not be reached.</exception>"""
    body: dict[str, Any] = {"blocking": blocking, "timer": None if blocking else seconds}
    return parse_blocking(session.call("POST", "/dns/blocking", body))


_sessions: dict[str, PiholeSession] = {}
_sessions_lock = threading.Lock()


def session_for(url: str, token_file: str | None, insecure: bool = False,
                request: Callable[..., tuple[int, Any]] = default_request) -> PiholeSession:
    """<summary>The one session kept for a box, made on the first ask.</summary>
    <param name="url">The box's address as the key wrote it.</param>
    <param name="token_file">Its credentials file, or None.</param>
    <param name="insecure">True to accept a certificate that does not verify.</param>
    <param name="request">How calls are made, for a session made here now.</param>
    <returns>The session, shared by every caller naming the address.</returns>
    <remarks>A changed credentials file or certificate setting on the key
    replaces the session, so a reload takes effect without a restart.</remarks>"""
    with _sessions_lock:
        session = _sessions.get(url)
        if session is None or session.token_file != token_file or session.insecure != insecure:
            session = PiholeSession(url, token_file, insecure, request=request)
            _sessions[url] = session
        return session


def default_prober(url: str, token_file: str | None, insecure: bool) -> PiholeReading:
    """<summary>The monitor's prober outside the tests: the shared session and the real box.</summary>
    <param name="url">The box's address.</param>
    <param name="token_file">Its credentials file, or None.</param>
    <param name="insecure">True to accept a certificate that does not verify.</param>
    <returns>The reading.</returns>"""
    return fetch(session_for(url, token_file, insecure))


class PiholeMonitor:
    """<summary>
    Polls every watched box on a background thread and remembers the last
    reading of each.
    </summary>
    <remarks>
    The lights monitor's shape: <see cref="watch"/> replaces the set of
    boxes at start and on every reload, <see cref="update"/> lets a press
    drop the state the box answered with straight in, so the lamp follows
    the press at once, and an unreachable box reads as None with the change
    logged once each way.

    The prober and the clock are injectable, and ``threaded=False`` keeps
    the thread from ever starting, so the tests drive <see cref="poll_once"/>
    by hand with no network.
    </remarks>
    """

    def __init__(self, *, prober: Callable[[str, str | None, bool], PiholeReading] = default_prober,
                 monotonic: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, threaded: bool = True) -> None:
        """<summary>An empty monitor, no thread yet.</summary>
        <param name="prober">Reads one box from its address, credentials file and certificate setting, or raises.</param>
        <param name="monotonic">The clock the schedule runs on.</param>
        <param name="log">Where reachability changes go.</param>
        <param name="threaded">False to never start the thread.</param>"""
        self.prober = prober
        self.monotonic = monotonic
        self.log = log
        self.threaded = threaded
        self._lock = threading.Lock()
        self._boxes: dict[str, tuple[str | None, bool, int]] = {}
        self._state: dict[str, PiholeReading | None] = {}
        self._due: dict[str, float] = {}
        self._asked: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake = threading.Event()

    def watch(self, boxes: list[tuple[str, str | None, bool, int]]) -> None:
        """<summary>Replace the set of boxes being asked.</summary>
        <param name="boxes">(address, credentials file, insecure, every_ms)
        for each. A repeated address keeps the shortest interval and the
        first of the rest.</param>"""
        with self._lock:
            fresh: dict[str, tuple[str | None, bool, int]] = {}
            for url, token_file, insecure, every_ms in boxes:
                if url in fresh:
                    old = fresh[url]
                    fresh[url] = (old[0], old[1], min(old[2], every_ms))
                else:
                    fresh[url] = (token_file, insecure, every_ms)
            for url in list(self._state):
                if url not in fresh:
                    del self._state[url]
                    self._due.pop(url, None)
                    self._asked.discard(url)
            for url in fresh:
                if url not in self._boxes:
                    self._due[url] = 0.0
                    self._state[url] = None
            self._boxes = fresh
        if fresh and self.threaded:
            self._wake.set()
            self._ensure_thread()

    def state(self, url: str) -> PiholeReading | None:
        """<summary>The last reading for a box.</summary>
        <param name="url">The address as the key wrote it.</param>
        <returns>The reading, or None when unreachable, not watched or not yet asked.</returns>"""
        with self._lock:
            return self._state.get(url)

    def update(self, url: str, blocking: bool, timer: int | None) -> None:
        """<summary>Record the blocking state a press got back, and push the next poll out.</summary>
        <param name="url">The box's address.</param>
        <param name="blocking">Whether blocking is on now.</param>
        <param name="timer">Seconds left of a pause, or None.</param>
        <remarks>The counts stay as they were: a press changes none of them,
        and the next poll brings them along.</remarks>"""
        with self._lock:
            if url in self._boxes and self._state.get(url) is not None:
                self._state[url] = replace(self._state[url], blocking=blocking, timer=timer)
                self._due[url] = self.monotonic() + self._boxes[url][2] / 1000

    def poll_once(self) -> float:
        """<summary>Ask every box that is due, and say when the next one is.</summary>
        <returns>Seconds until the earliest next ask, or 60 when nothing is watched.</returns>
        <remarks>The prober runs outside the lock, so a slow box never
        stalls <see cref="state"/>; only the bookkeeping is locked.</remarks>"""
        now = self.monotonic()
        with self._lock:
            due = [(url, box) for url, box in self._boxes.items() if self._due.get(url, 0.0) <= now]
        for url, (token_file, insecure, every_ms) in due:
            try:
                reading: PiholeReading | None = self.prober(url, token_file, insecure)
                why = ""
            except Exception as err:
                reading = None
                why = f" ({err})"
            with self._lock:
                if url not in self._boxes:
                    continue
                before = self._state.get(url)
                first = url not in self._asked
                self._asked.add(url)
                self._state[url] = reading
                self._due[url] = self.monotonic() + every_ms / 1000
            if first or (before is None) != (reading is None):
                self.log(f"pi-hole {url}: {'reachable' if reading is not None else 'unreachable'}{why}")
        with self._lock:
            if not self._boxes:
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
        self._thread = threading.Thread(target=self._run, name="pi-hole monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """<summary>The thread body: poll, sleep until the next one is due, repeat.</summary>"""
        while not self._stop_event.is_set():
            self._wake.clear()
            try:
                wait = self.poll_once()
            except Exception as err:  # a bug in the prober must not end the monitor
                self.log(f"pi-hole monitor: {err}")
                wait = 30.0
            self._wake.wait(wait)
