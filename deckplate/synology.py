"""<summary>
A Synology NAS, as far as a deck key needs it: the reading a key draws,
the sign in DSM wants, and a monitor that keeps the reading fresh.
</summary>
<remarks>
DSM answers its web API under ``/webapi/``: every call names an ``api``,
a ``version`` and a ``method`` in the query string, and the reply is JSON
with ``success`` and either ``data`` or an ``error`` carrying a ``code``.
Which cgi file answers an API, and which versions it speaks, come from
``SYNO.API.Info``, asked once per session as the official client does,
rather than being written here; DSM 7 answers everything used here from
``entry.cgi``.

Signing in is ``SYNO.API.Auth`` ``login`` with an account and its password,
sent as a POST body so the password never sits in a request line, and the
session id that comes back goes on every later call as ``_sid``. Error
119 is a session that has lapsed and 106 or 107 one DSM closed, and any of
them signs in once more and goes again. The reading itself is three calls:
``SYNO.Storage.CGI.Storage`` ``load_info`` for the volumes and disks,
``SYNO.Core.System.Utilization`` ``get`` for the processor and memory, and
``SYNO.DSM.Info`` ``getinfo`` for the model, temperature and uptime. The
calls and the field names are the ones the Home Assistant library reads,
checked against its source on 03/11/2026, and against the NAS's own
discovery reply.

A ``synology`` key names the box's address, the account and a credentials
file holding the password alone; the face is drawn from the latest reading
by <see cref="tiles.synology_tile"/> and the lamp in the corner says
whether everything the box reports is healthy. The press opens DSM.

An account with two step verification cannot sign in from here: DSM
answers 403, which is reported in those words. Use an account without it,
or a separate one made for the deck.

One session per box is kept here and renewed when the box says it has
lapsed. Polling runs on the monitor's thread, never on the deck's loop.
Nothing here touches the deck, and the password is read from its file on
every sign in and kept nowhere else.
</remarks>
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests
import urllib3

from .pihole import read_password

TIMEOUT_S = 4.0
DEFAULT_EVERY_MS = 30_000
SHOWS = ("space", "load", "system")
DOINGS = ("open",)
API_INFO = "SYNO.API.Info"
API_AUTH = "SYNO.API.Auth"
API_STORAGE = "SYNO.Storage.CGI.Storage"
API_UTILIZATION = "SYNO.Core.System.Utilization"
API_DSM_INFO = "SYNO.DSM.Info"
# The session has gone, by DSM's own numbering: sign in again and retry.
SESSION_ERRORS = (106, 107, 119)
# What a sign in refusal means, from DSM's auth error table.
AUTH_ERRORS = {
    400: "the account or password is wrong",
    401: "the account is disabled or a guest",
    402: "the account is not allowed in",
    403: "the account needs a one time code, which a key cannot give",
    404: "the one time code was refused",
    406: "the account must use a one time code, which a key cannot give",
    407: "too many tries; the box is blocking sign ins for a while",
    408: "the password has expired",
    409: "the password has expired",
    410: "the password must be changed first",
    411: "the account is locked",
}


@dataclass(frozen=True)
class Volume:
    """<summary>One volume as DSM reports it.</summary>
    <param name="id">DSM's own name, such as ``volume_1``.</param>
    <param name="status">DSM's word for its health; ``normal`` is the good one.</param>
    <param name="total">Its size in bytes.</param>
    <param name="used">Of that, how much is in use, in bytes.</param>"""

    id: str
    status: str
    total: int
    used: int

    @property
    def free(self) -> int:
        """<summary>Bytes still free, never negative.</summary>"""
        return max(0, self.total - self.used)


@dataclass(frozen=True)
class SynologyReading:
    """<summary>A NAS at one moment, as a key needs to know it.</summary>
    <param name="volumes">Every volume, in DSM's order.</param>
    <param name="disks_ok">Whether every disk reports a normal status.</param>
    <param name="disk_temp">The hottest disk in degrees, or None when unknown.</param>
    <param name="cpu">Processor load, 0 to 100, or None when unknown.</param>
    <param name="memory">Memory in use, 0 to 100, or None when unknown.</param>
    <param name="temperature">The system temperature in degrees, or None.</param>
    <param name="temperature_warn">Whether DSM is warning about the temperature.</param>
    <param name="uptime">Seconds since the box started, or None.</param>
    <param name="model">The model name, or "" when unknown.</param>"""

    volumes: tuple[Volume, ...]
    disks_ok: bool = True
    disk_temp: int | None = None
    cpu: int | None = None
    memory: int | None = None
    temperature: int | None = None
    temperature_warn: bool = False
    uptime: int | None = None
    model: str = ""

    def volume(self, name: str | None) -> Volume | None:
        """<summary>The named volume, or None when the box has no such volume.</summary>
        <param name="name">DSM's name for it, matched without regard to case.</param>"""
        if not name:
            return None
        for volume in self.volumes:
            if volume.id.lower() == name.lower():
                return volume
        return None

    def space(self, name: str | None = None) -> tuple[int, int] | None:
        """<summary>Free and total bytes for one volume, or for every volume together.</summary>
        <param name="name">A volume's name, or None for all of them summed.</param>
        <returns>(free, total), or None when the box has no such volume, or no volumes at all.</returns>"""
        if name:
            volume = self.volume(name)
            return None if volume is None else (volume.free, volume.total)
        if not self.volumes:
            return None
        return sum(v.free for v in self.volumes), sum(v.total for v in self.volumes)

    @property
    def healthy(self) -> bool:
        """<summary>Every volume and disk normal and no temperature warning.</summary>
        <remarks>What the lamp shows green for. A volume status other than
        ``normal`` is DSM telling the owner something, degraded or crashed
        among them, and the lamp going red is the whole point of a key for
        a box that sits in a cupboard.</remarks>"""
        return (all(v.status == "normal" for v in self.volumes) and self.disks_ok
                and not self.temperature_warn)


def api_url(url: str, cgi: str) -> str:
    """<summary>A web API address under a box's root.</summary>
    <param name="url">The address as the key wrote it: the DSM root, with or
    without a trailing slash.</param>
    <param name="cgi">The cgi file, such as ``entry.cgi``.</param>
    <returns>The full address.</returns>"""
    return url.rstrip("/") + "/webapi/" + cgi


def _number(value: Any, default: int | None = None) -> int | None:
    """<summary>An int from what DSM sent, which is a string as often as a number.</summary>
    <param name="value">The value as decoded.</param>
    <param name="default">What to answer for something that is not a number.</param>"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_storage(payload: Any) -> tuple[tuple[Volume, ...], bool, int | None]:
    """<summary>Read a ``load_info`` reply's data.</summary>
    <param name="payload">The ``data`` of the decoded reply.</param>
    <returns>(volumes, every disk normal, hottest disk temperature or None).</returns>
    <remarks>Sizes arrive as strings of bytes and are read as such; a volume
    missing its size is kept at zero rather than dropped, so a face can
    still say the volume is there and unwell.</remarks>
    <exception cref="ValueError">The reply is not a storage reply.</exception>"""
    if not isinstance(payload, dict) or not isinstance(payload.get("volumes"), list):
        raise ValueError("not a DSM storage reply")
    volumes = []
    for raw in payload["volumes"]:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        size = raw.get("size") if isinstance(raw.get("size"), dict) else {}
        volumes.append(Volume(str(raw["id"]), str(raw.get("status") or "unknown"),
                              max(0, _number(size.get("total"), 0) or 0), max(0, _number(size.get("used"), 0) or 0)))
    disks = payload.get("disks") if isinstance(payload.get("disks"), list) else []
    disks_ok = all(not isinstance(d, dict) or d.get("status", "normal") == "normal" for d in disks)
    temps = [t for t in (_number(d.get("temp")) for d in disks if isinstance(d, dict)) if t is not None]
    return tuple(volumes), disks_ok, (max(temps) if temps else None)


def parse_utilization(payload: Any) -> tuple[int | None, int | None]:
    """<summary>Read a utilisation reply's data.</summary>
    <param name="payload">The ``data`` of the decoded reply.</param>
    <returns>(processor load percent, memory in use percent), either None when absent.</returns>
    <remarks>The processor figure is the three loads DSM splits it into
    added together, as the Resource Monitor shows it; the memory figure is
    DSM's own ``real_usage`` percentage.</remarks>"""
    if not isinstance(payload, dict):
        return None, None
    cpu = payload.get("cpu") if isinstance(payload.get("cpu"), dict) else None
    memory = payload.get("memory") if isinstance(payload.get("memory"), dict) else None
    load = None
    if cpu is not None:
        parts = [_number(cpu.get(k)) for k in ("user_load", "system_load", "other_load")]
        if any(p is not None for p in parts):
            load = max(0, min(100, sum(p or 0 for p in parts)))
    used = None
    if memory is not None and _number(memory.get("real_usage")) is not None:
        used = max(0, min(100, _number(memory.get("real_usage")) or 0))
    return load, used


def parse_info(payload: Any) -> tuple[str, int | None, bool, int | None]:
    """<summary>Read a DSM info reply's data.</summary>
    <param name="payload">The ``data`` of the decoded reply.</param>
    <returns>(model, temperature or None, temperature warning, uptime seconds or None).</returns>"""
    if not isinstance(payload, dict):
        return "", None, False, None
    return (str(payload.get("model") or ""), _number(payload.get("temperature")),
            bool(payload.get("temperature_warn", False)), _number(payload.get("uptime")))


def default_request(method: str, url: str, params: dict, data: dict | None, timeout: float, verify: bool) -> Any:
    """<summary>One HTTP call to DSM, answered as the decoded JSON.</summary>
    <param name="method">GET or POST.</param>
    <param name="url">The cgi address.</param>
    <param name="params">The query string: api, version, method and the rest.</param>
    <param name="data">A form body for a POST, or None.</param>
    <param name="timeout">Seconds to wait.</param>
    <param name="verify">False to accept a certificate that does not verify.</param>
    <returns>The decoded body.</returns>
    <remarks>DSM answers every refusal as a 200 with ``success`` false and
    an error code inside, so the status is only checked for the box being
    there at all; the code is the caller's to read.</remarks>
    <exception cref="requests.RequestException">The box could not be reached or answered an error status.</exception>
    <exception cref="ValueError">The reply was not JSON.</exception>"""
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = requests.request(method, url, params=params, data=data, headers={"User-Agent": "deckplate"},
                                timeout=timeout, verify=verify)
    response.raise_for_status()
    return response.json()


class SynologySession:
    """<summary>
    One box's session: learns where its APIs live, signs in when it has to,
    and puts the session id on every call.
    </summary>
    <remarks>
    The session id is kept here and nowhere else; the password is read from
    its file each time a sign in is needed. A call that DSM answers with a
    session error signs in once and goes again. Shared by the monitor's
    thread and a press on the deck's loop, so every call is under a lock.
    </remarks>
    """

    def __init__(self, url: str, account: str | None, token_file: str | None, insecure: bool = False,
                 request: Callable[..., Any] = default_request,
                 read_password: Callable[[str], str] = read_password) -> None:
        """<summary>A session for one box, not yet signed in.</summary>
        <param name="url">The box's address as the key wrote it.</param>
        <param name="account">The DSM account to sign in as, or None for a
        key still being set up, which cannot sign in and says so.</param>
        <param name="token_file">The credentials file holding the password, or None.</param>
        <param name="insecure">True to accept a certificate that does not verify.</param>
        <param name="request">How calls are made. Swapped in the tests.</param>
        <param name="read_password">How the password is read.</param>"""
        self.url = url
        self.account = account
        self.token_file = token_file
        self.insecure = insecure
        self.request = request
        self.read_password = read_password
        self.sid: str | None = None
        self.apis: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _discover(self) -> None:
        """<summary>Ask the box which cgi answers each API and which versions it speaks.</summary>
        <exception cref="ValueError">The reply was not a DSM discovery reply.</exception>"""
        wanted = ",".join((API_AUTH, API_STORAGE, API_UTILIZATION, API_DSM_INFO))
        reply = self.request("GET", api_url(self.url, "query.cgi"),
                             {"api": API_INFO, "version": 1, "method": "query", "query": wanted}, None,
                             TIMEOUT_S, not self.insecure)
        data = reply.get("data") if isinstance(reply, dict) and reply.get("success") else None
        if not isinstance(data, dict) or API_AUTH not in data:
            raise ValueError("not a DSM box: the API query was not answered")
        self.apis = {name: info for name, info in data.items() if isinstance(info, dict)}

    def _api(self, name: str) -> tuple[str, int]:
        """<summary>The cgi and the highest version the box offers for an API.</summary>
        <exception cref="ValueError">The box does not offer it.</exception>"""
        info = self.apis.get(name)
        if not info:
            raise ValueError(f"the box does not offer {name}")
        return str(info.get("path") or "entry.cgi"), int(info.get("maxVersion") or 1)

    def _login(self) -> None:
        """<summary>Sign in and keep the session id.</summary>
        <exception cref="ValueError">The box refused the sign in, with DSM's reason.</exception>"""
        if not self.token_file:
            raise ValueError("the box wants a password and the key names no credentials file")
        if not self.account:
            raise ValueError("the key names no account")
        password = self.read_password(self.token_file)
        cgi, version = self._api(API_AUTH)
        reply = self.request("POST", api_url(self.url, cgi), {"api": API_AUTH, "version": version, "method": "login"},
                             {"account": self.account, "passwd": password, "format": "sid"}, TIMEOUT_S, not self.insecure)
        data = reply.get("data") if isinstance(reply, dict) and reply.get("success") else None
        if not isinstance(data, dict) or not data.get("sid"):
            code = _error_code(reply)
            raise ValueError("DSM refused the sign in: " + AUTH_ERRORS.get(code, f"error {code}"))
        self.sid = str(data["sid"])

    def call(self, api: str, method: str, params: dict | None = None) -> Any:
        """<summary>Make one API call, signing in first or again as needed.</summary>
        <param name="api">The API name, such as ``SYNO.DSM.Info``.</param>
        <param name="method">Its method, such as ``getinfo``.</param>
        <param name="params">Further query parameters, or None.</param>
        <returns>The reply's ``data``.</returns>
        <exception cref="ValueError">The box refused the sign in or the call.</exception>
        <exception cref="requests.RequestException">The box could not be reached.</exception>"""
        with self._lock:
            if not self.apis:
                self._discover()
            cgi, version = self._api(api)
            for attempt in (1, 2):
                if self.sid is None:
                    self._login()
                query = {"api": api, "version": version, "method": method, "_sid": self.sid, **(params or {})}
                reply = self.request("GET", api_url(self.url, cgi), query, None, TIMEOUT_S, not self.insecure)
                if isinstance(reply, dict) and reply.get("success"):
                    return reply.get("data")
                code = _error_code(reply)
                if code in SESSION_ERRORS and attempt == 1:
                    self.sid = None
                    continue
                raise ValueError(f"DSM refused {api} {method}: error {code}")
            raise ValueError(f"DSM refused {api} {method} after signing in again")


def _error_code(reply: Any) -> int:
    """<summary>The error code inside a DSM refusal, or 0 when there is none.</summary>"""
    if isinstance(reply, dict) and isinstance(reply.get("error"), dict):
        return _number(reply["error"].get("code"), 0) or 0
    return 0


def fetch(session: SynologySession) -> SynologyReading:
    """<summary>Ask a box for its volumes, load and temperature.</summary>
    <param name="session">The box's session.</param>
    <returns>The reading.</returns>
    <remarks>The storage call is the one that must answer; the other two
    are allowed to fail, with their figures left unknown, because an
    account kept out of the Resource Monitor still has volumes worth
    showing.</remarks>
    <exception cref="ValueError">The storage reply was not DSM's, or the sign in was refused.</exception>
    <exception cref="requests.RequestException">The box could not be reached.</exception>"""
    volumes, disks_ok, disk_temp = parse_storage(session.call(API_STORAGE, "load_info"))
    cpu = memory = temperature = uptime = None
    warn, model = False, ""
    try:
        cpu, memory = parse_utilization(session.call(API_UTILIZATION, "get"))
    except ValueError:
        pass
    try:
        model, temperature, warn, uptime = parse_info(session.call(API_DSM_INFO, "getinfo"))
    except ValueError:
        pass
    return SynologyReading(volumes, disks_ok, disk_temp, cpu, memory, temperature, warn, uptime, model)


_sessions: dict[str, SynologySession] = {}
_sessions_lock = threading.Lock()


def session_for(url: str, account: str | None, token_file: str | None, insecure: bool = False,
                request: Callable[..., Any] = default_request) -> SynologySession:
    """<summary>The one session kept for a box, made on the first ask.</summary>
    <param name="url">The box's address as the key wrote it.</param>
    <param name="account">The account to sign in as.</param>
    <param name="token_file">Its credentials file, or None.</param>
    <param name="insecure">True to accept a certificate that does not verify.</param>
    <param name="request">How calls are made, for a session made here now.</param>
    <returns>The session, shared by every caller naming the address.</returns>
    <remarks>A changed account, credentials file or certificate setting on
    the key replaces the session, so a reload takes effect without a restart.</remarks>"""
    with _sessions_lock:
        session = _sessions.get(url)
        if (session is None or session.account != account or session.token_file != token_file
                or session.insecure != insecure):
            session = SynologySession(url, account, token_file, insecure, request=request)
            _sessions[url] = session
        return session


def default_prober(url: str, account: str | None, token_file: str | None, insecure: bool) -> SynologyReading:
    """<summary>The monitor's prober outside the tests: the shared session and the real box.</summary>
    <param name="url">The box's address.</param>
    <param name="account">The account.</param>
    <param name="token_file">Its credentials file, or None.</param>
    <param name="insecure">True to accept a certificate that does not verify.</param>
    <returns>The reading.</returns>"""
    return fetch(session_for(url, account, token_file, insecure))


class SynologyMonitor:
    """<summary>
    Polls every watched box on a background thread and remembers the last
    reading of each.
    </summary>
    <remarks>
    The Pi-hole monitor's shape, without the press update it needs, since
    a synology key's press changes nothing on the box: <see cref="watch"/>
    replaces the set of boxes at start and on every reload, and an
    unreachable box reads as None with the change logged once each way.

    The prober and the clock are injectable, and ``threaded=False`` keeps
    the thread from ever starting, so the tests drive <see cref="poll_once"/>
    by hand with no network.
    </remarks>
    """

    def __init__(self, *, prober: Callable[[str, str | None, str | None, bool], SynologyReading] = default_prober,
                 monotonic: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, threaded: bool = True) -> None:
        """<summary>An empty monitor, no thread yet.</summary>
        <param name="prober">Reads one box from its address, account, credentials file and certificate setting, or raises.</param>
        <param name="monotonic">The clock the schedule runs on.</param>
        <param name="log">Where reachability changes go.</param>
        <param name="threaded">False to never start the thread.</param>"""
        self.prober = prober
        self.monotonic = monotonic
        self.log = log
        self.threaded = threaded
        self._lock = threading.Lock()
        self._boxes: dict[str, tuple[str | None, str | None, bool, int]] = {}
        self._state: dict[str, SynologyReading | None] = {}
        self._due: dict[str, float] = {}
        self._asked: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake = threading.Event()

    def watch(self, boxes: list[tuple[str, str | None, str | None, bool, int]]) -> None:
        """<summary>Replace the set of boxes being asked.</summary>
        <param name="boxes">(address, account, credentials file, insecure,
        every_ms) for each. A repeated address keeps the shortest interval
        and the first of the rest.</param>"""
        with self._lock:
            fresh: dict[str, tuple[str | None, str | None, bool, int]] = {}
            for url, account, token_file, insecure, every_ms in boxes:
                if url in fresh:
                    old = fresh[url]
                    fresh[url] = (old[0], old[1], old[2], min(old[3], every_ms))
                else:
                    fresh[url] = (account, token_file, insecure, every_ms)
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

    def state(self, url: str) -> SynologyReading | None:
        """<summary>The last reading for a box.</summary>
        <param name="url">The address as the key wrote it.</param>
        <returns>The reading, or None when unreachable, not watched or not yet asked.</returns>"""
        with self._lock:
            return self._state.get(url)

    def poll_once(self) -> float:
        """<summary>Ask every box that is due, and say when the next one is.</summary>
        <returns>Seconds until the earliest next ask, or 60 when nothing is watched.</returns>
        <remarks>The prober runs outside the lock, so a slow box never
        stalls <see cref="state"/>; only the bookkeeping is locked.</remarks>"""
        now = self.monotonic()
        with self._lock:
            due = [(url, box) for url, box in self._boxes.items() if self._due.get(url, 0.0) <= now]
        for url, (account, token_file, insecure, every_ms) in due:
            try:
                reading: SynologyReading | None = self.prober(url, account, token_file, insecure)
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
                self.log(f"synology {url}: {'reachable' if reading is not None else 'unreachable'}{why}")
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
        self._thread = threading.Thread(target=self._run, name="synology monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """<summary>The thread body: poll, sleep until the next one is due, repeat.</summary>"""
        while not self._stop_event.is_set():
            self._wake.clear()
            try:
                wait = self.poll_once()
            except Exception as err:  # a bug in the prober must not end the monitor
                self.log(f"synology monitor: {err}")
                wait = 30.0
            self._wake.wait(wait)
