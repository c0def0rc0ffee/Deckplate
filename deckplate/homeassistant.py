"""<summary>
Home Assistant, as far as a deck key needs it: one entity's state as the
reading a key draws, the service call a press makes, and a monitor that
keeps every watched entity fresh.
</summary>
<remarks>
Home Assistant answers a REST API under ``/api`` with a long lived access
token in the ``Authorization: Bearer`` header. ``GET /api/states/<entity>``
gives an entity's state, its friendly name and its attributes, and ``POST
/api/services/<domain>/<service>`` with a JSON body calls a service and
answers with the states it changed. There is no session to keep: every
call carries the token, which is read from its file each time and kept
nowhere else.

A ``homeassistant`` key names the server's address, the token file and
one entity. The face is drawn from the latest reading by
<see cref="tiles.homeassistant_tile"/>, the lamp in the corner follows an
entity that is on or off, and the press toggles it, turns it on or off,
calls a service of the key's choosing, or opens Home Assistant.

Polling runs on the monitor's thread, never on the deck's loop. Nothing
here touches the deck.
</remarks>
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests
import urllib3

TIMEOUT_S = 4.0
DEFAULT_EVERY_MS = 10_000
DOINGS = ("toggle", "turn_on", "turn_off", "service", "open")
# States that read as on and as off, for the lamp. Anything else is a
# figure or a word, drawn but given no lamp.
ON_STATES = frozenset({"on", "open", "unlocked", "home", "playing", "heat", "cool", "heat_cool", "auto", "cleaning", "docked"})
OFF_STATES = frozenset({"off", "closed", "locked", "not_home", "idle", "paused", "standby", "unavailable", "unknown"})


@dataclass(frozen=True)
class HomeAssistantReading:
    """<summary>One entity as a key needs to know it.</summary>
    <param name="entity">The entity id, such as ``light.lounge``.</param>
    <param name="state">The state as Home Assistant gives it: ``on``, ``21.4``, ``home``.</param>
    <param name="name">The friendly name, or the entity id when it has none.</param>
    <param name="unit">The unit of measurement, or an empty string.</param>
    <param name="icon">The ``mdi:`` icon name Home Assistant holds for it, or an empty string. Not drawn; kept for the page.</param>
    <param name="device_class">What Home Assistant says the entity measures, or an empty string. ``timestamp`` marks a state that is a date and time.</param>"""

    entity: str
    state: str
    name: str
    unit: str = ""
    icon: str = ""
    device_class: str = ""

    @property
    def on(self) -> bool | None:
        """<summary>True for an on state, False for an off one, None for a figure or a word that is neither.</summary>"""
        if self.state in ON_STATES:
            return True
        if self.state in OFF_STATES:
            return False
        return None

    @property
    def domain(self) -> str:
        """<summary>The part of the entity id before the dot: ``light``, ``sensor``.</summary>"""
        return self.entity.split(".", 1)[0]

    @property
    def text(self) -> str:
        """<summary>The state as words for the face: a figure with its unit, or a state word with its underscores gone and its first letter up.</summary>"""
        if self.on is None and self.unit:
            return f"{self.state} {self.unit}".strip()
        return self.state.replace("_", " ").capitalize()

    @property
    def moment(self) -> datetime | None:
        """<summary>The state as a date and time in the machine's own zone, or None when it is not one.</summary>
        <remarks>Only an entity marked ``timestamp`` counts, so a text sensor
        that happens to hold a date stays as written. A timestamp entity
        that has no value reads ``unknown`` or ``unavailable``, which gives
        None and is drawn as that word.</remarks>"""
        if self.device_class != "timestamp":
            return None
        try:
            return datetime.fromisoformat(self.state.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return None

    def words(self, now: datetime) -> str:
        """<summary>The state as the face shows it at a given moment.</summary>
        <param name="now">The moment to measure from, passed in so a tile can
        be drawn at a fixed time in a test. Naive means the machine's zone.</param>
        <returns><see cref="text"/> for anything but a timestamp. A coming
        time within a day is the clock time, within a week the weekday and
        clock time, and further off the date. A past time is its age, or
        the date once it is over a month old.</returns>
        <remarks>Whole minutes at the finest, so a key showing an age changes
        once a minute at most, the same rate as a clock on the deck.</remarks>"""
        when = self.moment
        if when is None:
            return self.text
        seconds = (when - now.astimezone()).total_seconds()
        date = f"{when.day} {when:%b}"
        if seconds >= 0:
            if seconds < 86_400:
                return f"{when:%H:%M}"
            return f"{when:%a %H:%M}" if seconds < 7 * 86_400 else date
        age = -seconds
        if age < 60:
            return "just now"
        if age < 3_600:
            return f"{int(age // 60)} min ago"
        if age < 86_400:
            return f"{int(age // 3_600)} h ago"
        return f"{int(age // 86_400)} d ago" if age < 30 * 86_400 else date


def api_url(url: str, path: str) -> str:
    """<summary>An API address from the server's address and a path under ``/api``.</summary>
    <param name="url">The server's address as the key wrote it, with or without a trailing slash.</param>
    <param name="path">The part after ``/api``, starting with a slash.</param>
    <returns>The whole address.</returns>"""
    return url.rstrip("/") + "/api" + path


def read_token(path: str) -> str:
    """<summary>The long lived access token from its file, whitespace trimmed.</summary>
    <param name="path">The file's path, ``~`` allowed.</param>
    <returns>The token.</returns>
    <exception cref="ValueError">The file is missing or empty.</exception>
    <remarks>Read on every call and kept nowhere, as the Pi-hole password is.</remarks>"""
    try:
        token = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError as err:
        raise ValueError(f"token file {path}: {err.strerror or err}") from None
    if not token:
        raise ValueError(f"token file {path} is empty")
    return token


def parse_state(payload: Any) -> HomeAssistantReading:
    """<summary>A reading from the JSON ``/api/states/<entity>`` answers with.</summary>
    <param name="payload">The decoded answer.</param>
    <returns>The reading.</returns>
    <exception cref="ValueError">The answer is not an entity state.</exception>"""
    if not isinstance(payload, dict) or not isinstance(payload.get("entity_id"), str):
        raise ValueError("not an entity state")
    attributes = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
    entity = payload["entity_id"]
    return HomeAssistantReading(entity=entity, state=str(payload.get("state", "unknown")),
                                name=str(attributes.get("friendly_name") or entity),
                                unit=str(attributes.get("unit_of_measurement") or ""),
                                icon=str(attributes.get("icon") or ""),
                                device_class=str(attributes.get("device_class") or ""))


def default_request(method: str, url: str, headers: dict, body: dict | None, timeout: float, verify: bool) -> tuple[int, Any]:
    """<summary>One HTTP call, answered as a status and the decoded JSON.</summary>
    <param name="method">GET or POST.</param>
    <param name="url">The address.</param>
    <param name="headers">Headers, the bearer token among them.</param>
    <param name="body">A JSON body, or None.</param>
    <param name="timeout">Seconds to wait.</param>
    <param name="verify">False to accept a certificate that does not verify.</param>
    <returns>(status code, decoded body or None).</returns>
    <exception cref="requests.RequestException">The server could not be reached or refused.</exception>
    <remarks>A 401 or 403 is raised as a refusal with the status in its
    text, so a bad token is a clear line in the log rather than a reading
    that never comes.</remarks>"""
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    sent = {"User-Agent": "deckplate", **headers}
    response = requests.request(method, url, headers=sent, json=body, timeout=timeout, verify=verify)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    response.raise_for_status()
    return response.status_code, payload


class HomeAssistantServer:
    """<summary>
    One Home Assistant server: its address, its token file and how it is called.
    </summary>
    <remarks>Stateless beyond those three, since every call carries the
    token. The request door is injectable so the tests record calls
    instead of making them.</remarks>
    """

    def __init__(self, url: str, token_file: str | None, insecure: bool = False,
                 request: Callable[..., tuple[int, Any]] = default_request) -> None:
        """<summary>A server by address.</summary>
        <param name="url">The server's address as the key wrote it.</param>
        <param name="token_file">The file holding the long lived access token, or None.</param>
        <param name="insecure">True to accept a certificate that does not verify.</param>
        <param name="request">How calls are made.</param>"""
        self.url = url
        self.token_file = token_file
        self.insecure = insecure
        self.request = request

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        """<summary>One API call with the token.</summary>
        <param name="method">GET or POST.</param>
        <param name="path">The part after ``/api``.</param>
        <param name="body">A JSON body, or None.</param>
        <returns>The decoded answer.</returns>
        <exception cref="ValueError">No token file is set, or it cannot be read.</exception>
        <exception cref="requests.RequestException">The server could not be reached or refused.</exception>"""
        if not self.token_file:
            raise ValueError("no token file: make a long lived access token in Home Assistant and write it to a file")
        headers = {"Authorization": "Bearer " + read_token(self.token_file)}
        _status, payload = self.request(method, api_url(self.url, path), headers, body, TIMEOUT_S, not self.insecure)
        return payload


def fetch(server: HomeAssistantServer, entity: str) -> HomeAssistantReading:
    """<summary>One entity's reading from the server.</summary>
    <param name="server">The server.</param>
    <param name="entity">The entity id.</param>
    <returns>The reading.</returns>
    <exception cref="ValueError">The entity is not known or the answer is not a state.</exception>"""
    return parse_state(server.call("GET", "/states/" + entity))


def call_service(server: HomeAssistantServer, service: str, data: dict | None = None) -> list[HomeAssistantReading]:
    """<summary>Call a service and say which entities it changed.</summary>
    <param name="server">The server.</param>
    <param name="service">``domain.service``, such as ``light.turn_on`` or ``homeassistant.toggle``.</param>
    <param name="data">The service data, ``entity_id`` among it, or None.</param>
    <returns>The readings of the entities whose state changed, possibly none.</returns>
    <exception cref="ValueError">The service name has no dot.</exception>"""
    domain, _, name = service.partition(".")
    if not domain or not name:
        raise ValueError(f"service must be domain.service, not '{service}'")
    answer = server.call("POST", f"/services/{domain}/{name}", data or {})
    readings = []
    if isinstance(answer, list):
        for item in answer:
            try:
                readings.append(parse_state(item))
            except ValueError:
                continue
    return readings


_servers: dict[str, HomeAssistantServer] = {}
_servers_lock = threading.Lock()


def server_for(url: str, token_file: str | None, insecure: bool = False,
               request: Callable[..., tuple[int, Any]] = default_request) -> HomeAssistantServer:
    """<summary>The one server object kept for an address, made on the first ask.</summary>
    <param name="url">The server's address as the key wrote it.</param>
    <param name="token_file">Its token file, or None.</param>
    <param name="insecure">True to accept a certificate that does not verify.</param>
    <param name="request">How calls are made, for a server made here now.</param>
    <returns>The server, shared by every caller naming the address.</returns>
    <remarks>A changed token file or certificate setting on the key replaces
    it, so a reload takes effect without a restart.</remarks>"""
    with _servers_lock:
        server = _servers.get(url)
        if server is None or server.token_file != token_file or server.insecure != insecure:
            server = HomeAssistantServer(url, token_file, insecure, request=request)
            _servers[url] = server
        return server


def default_prober(url: str, token_file: str | None, insecure: bool, entity: str) -> HomeAssistantReading:
    """<summary>The monitor's prober outside the tests: the shared server and the real box.</summary>
    <param name="url">The server's address.</param>
    <param name="token_file">Its token file, or None.</param>
    <param name="insecure">True to accept a certificate that does not verify.</param>
    <param name="entity">The entity id.</param>
    <returns>The reading.</returns>"""
    return fetch(server_for(url, token_file, insecure), entity)


Watched = tuple[str, str]  # (server address, entity id)


class HomeAssistantMonitor:
    """<summary>
    Polls every watched entity on a background thread and remembers the
    last reading of each.
    </summary>
    <remarks>
    The Pi-hole monitor's shape, keyed by server and entity rather than
    server alone, since one server answers for many keys: <see cref="watch"/>
    replaces the set at start and on every reload, <see cref="update"/>
    lets a press drop the state the server answered with straight in, so
    the face and the lamp follow the press at once, and an unreachable
    entity reads as None with the change logged once each way.

    The prober and the clock are injectable, and ``threaded=False`` keeps
    the thread from ever starting, so the tests drive <see cref="poll_once"/>
    by hand with no network.
    </remarks>
    """

    def __init__(self, *, prober: Callable[[str, str | None, bool, str], HomeAssistantReading] = default_prober,
                 monotonic: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, threaded: bool = True) -> None:
        """<summary>An empty monitor, no thread yet.</summary>
        <param name="prober">Reads one entity from the server's address, token file, certificate setting and the entity id, or raises.</param>
        <param name="monotonic">The clock the schedule runs on.</param>
        <param name="log">Where reachability changes go.</param>
        <param name="threaded">False to never start the thread.</param>"""
        self.prober = prober
        self.monotonic = monotonic
        self.log = log
        self.threaded = threaded
        self._lock = threading.Lock()
        self._watched: dict[Watched, tuple[str | None, bool, int]] = {}
        self._state: dict[Watched, HomeAssistantReading | None] = {}
        self._due: dict[Watched, float] = {}
        self._asked: set[Watched] = set()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake = threading.Event()

    def watch(self, entities: list[tuple[str, str | None, bool, str, int]]) -> None:
        """<summary>Replace the set of entities being asked.</summary>
        <param name="entities">(address, token file, insecure, entity id, every_ms)
        for each. A repeated pair keeps the shortest interval and the first
        of the rest.</param>"""
        with self._lock:
            fresh: dict[Watched, tuple[str | None, bool, int]] = {}
            for url, token_file, insecure, entity, every_ms in entities:
                at = (url, entity)
                if at in fresh:
                    old = fresh[at]
                    fresh[at] = (old[0], old[1], min(old[2], every_ms))
                else:
                    fresh[at] = (token_file, insecure, every_ms)
            for at in list(self._state):
                if at not in fresh:
                    del self._state[at]
                    self._due.pop(at, None)
                    self._asked.discard(at)
            for at in fresh:
                if at not in self._watched:
                    self._due[at] = 0.0
                    self._state[at] = None
            self._watched = fresh
        if fresh and self.threaded:
            self._wake.set()
            self._ensure_thread()

    def state(self, url: str, entity: str) -> HomeAssistantReading | None:
        """<summary>The last reading for an entity.</summary>
        <param name="url">The server's address as the key wrote it.</param>
        <param name="entity">The entity id.</param>
        <returns>The reading, or None when unreachable, not watched or not yet asked.</returns>"""
        with self._lock:
            return self._state.get((url, entity))

    def update(self, url: str, reading: HomeAssistantReading) -> None:
        """<summary>Record a reading a press got back, and push the next poll out.</summary>
        <param name="url">The server's address.</param>
        <param name="reading">The entity's state as the service call answered it.</param>
        <remarks>An entity the service changed but no key watches is passed over.</remarks>"""
        at = (url, reading.entity)
        with self._lock:
            if at in self._watched:
                self._state[at] = reading
                self._due[at] = self.monotonic() + self._watched[at][2] / 1000

    def poll_once(self) -> float:
        """<summary>Ask every entity that is due, and say when the next one is.</summary>
        <returns>Seconds until the earliest next ask, or 60 when nothing is watched.</returns>
        <remarks>The prober runs outside the lock, so a slow server never
        stalls <see cref="state"/>; only the bookkeeping is locked.</remarks>"""
        now = self.monotonic()
        with self._lock:
            due = [(at, box) for at, box in self._watched.items() if self._due.get(at, 0.0) <= now]
        for (url, entity), (token_file, insecure, every_ms) in due:
            try:
                reading: HomeAssistantReading | None = self.prober(url, token_file, insecure, entity)
                why = ""
            except Exception as err:
                reading = None
                why = f" ({err})"
            at = (url, entity)
            with self._lock:
                if at not in self._watched:
                    continue
                before = self._state.get(at)
                first = at not in self._asked
                self._asked.add(at)
                self._state[at] = reading
                self._due[at] = self.monotonic() + every_ms / 1000
            if first or (before is None) != (reading is None):
                self.log(f"home assistant {url} {entity}: {'reachable' if reading is not None else 'unreachable'}{why}")
        with self._lock:
            if not self._watched:
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
        self._thread = threading.Thread(target=self._run, name="home assistant monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """<summary>The thread body: poll, sleep until the next one is due, repeat.</summary>"""
        while not self._stop_event.is_set():
            self._wake.clear()
            try:
                wait = self.poll_once()
            except Exception as err:  # a bug in the prober must not end the monitor
                self.log(f"home assistant monitor: {err}")
                wait = 30.0
            self._wake.wait(wait)


def parse_data(text: str | None) -> dict:
    """<summary>A service's extra data from the JSON text a key carries.</summary>
    <param name="text">A JSON object as text, or None or empty for none.</param>
    <returns>The data, possibly empty.</returns>
    <exception cref="ValueError">The text is not a JSON object.</exception>"""
    if not text or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("'data' must be a JSON object") from None
    if not isinstance(data, dict):
        raise ValueError("'data' must be a JSON object")
    return data
