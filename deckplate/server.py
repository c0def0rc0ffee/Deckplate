"""<summary>
The local HTTP channel the configuration page talks to.
</summary>
<remarks>
Standard library only. Runs in its own thread beside the controller loop and
never touches the deck directly: reads come from the controller's snapshot
and tile cache, and anything that changes state is queued for the controller
thread to run. A server sent events stream pushes key presses, page changes
and tile updates to the page as they happen.

Bound to 127.0.0.1 unless the config says otherwise, in which case a token is
required on every request (header X-Token or ?token=).

Browsers will happily send requests to 127.0.0.1 from any website the user
has open, so three checks keep other sites out: the Origin header, when
present, must be this server; the Host header must be a loopback name; and
POST and PUT bodies must carry a content type that browsers refuse to send
across origins without a preflight, which this server never grants.

Those three checks are the whole defence and they are only as good as their
weakest link, so none of them may be relaxed for convenience. The Origin
check is skipped when the header is absent, because a command line client
sends none; a browser always does, which is what makes that safe. The Host
check only applies on a loopback bind, where any other Host means the request
arrived through something rewriting it. The content type check is the one
that stops a plain HTML form posting to this server from any page on the
internet, since a form can only send three content types and none of them is
application/json. Granting CORS, or accepting a form content type on a POST,
would undo it: <see cref="ApiHandler.do_OPTIONS"/> answers no preflight on
purpose.

A token is optional on loopback and required off it, and the token is the
only thing standing between the deck and the rest of the network once the
bind is widened. It is accepted in the query string as well as the header so
that an image or event stream URL can carry it, which is a real weakening:
query strings end up in logs and in browser history. Loopback, the default,
is the configuration to prefer.

No route here writes to the deck. Reads are served from the controller's
snapshot and its tile cache, and every change is queued with ``submit`` for
the controller thread, so the USB device is touched by one thread only and
the safety gates in the protocol and device modules are never bypassed. The
preview routes render images for the page alone and send nothing to the
hardware, at any scale.

Routes:
    GET  /                          the configuration page, or a placeholder
    GET  /api/state                 JSON snapshot of the controller
    GET  /api/tiles/<row>/<col>.jpg the tile as shown on that panel, upright.
    GET  /api/tiles/<row>/<col>.gif the whole loop of an animated panel; 404 if still.
                                    ?scale=1..3 draws it that many times the deck's
                                    image size, for the page, which shows the panels
                                    far larger than the deck does. Never sent to the deck.
    GET  /api/config                the config file text
    PUT  /api/config                replace the config file (validated first), application/toml
    GET  /api/document              the config as JSON, the form the page edits
    PUT  /api/document              write the config from JSON (validated first)
    GET  /api/images                the pictures in the images folder, with what uses each
    GET  /api/images/<name>         one picture
    POST /api/images                upload a picture: body is the file, header X-Filename
    DELETE /api/images/<name>       remove a picture that nothing uses
    GET  /api/geocode?name=Town     coordinates for a place name, for the weather tile
    GET  /api/events                server sent events stream
    POST /api/page      {"page": "Name" | "next" | "previous"}
    POST /api/brightness {"value": 0..100} or {"delta": n}
    POST /api/press     {"row": r, "column": c, "gesture": "press"|"long"|"double"}
                                    act as if that key was pressed; the gesture
                                    picks the plain, long press or double press action
    POST /api/wake
    POST /api/sleep
POST bodies are application/json.
</remarks>
"""

from __future__ import annotations

import io
import json
import os
import queue
import select
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import layout, themes
from .controller import MAX_PREVIEW_SCALE
from .config import LOOPBACK_ADDRESSES, ConfigError, ServerSettings, document_from_config, parse, to_toml

WEB_DIR = Path(__file__).with_name("web")
PING_SECONDS = 15
MAX_BODY_BYTES = 8 * 1024 * 1024
JSON_TYPE = "application/json"
TOML_TYPES = ("application/toml", "text/toml")
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
PLACEHOLDER = """<!doctype html>
<title>Deckplate</title>
<style>body{font:15px system-ui,sans-serif;margin:3em;color:#222}code{background:#eee;padding:2px 5px}</style>
<h1>Deckplate is running</h1>
<p>The configuration page is not built yet. The API is live:
<code>/api/state</code>, <code>/api/tiles/0/0.jpg</code>, <code>/api/config</code>, <code>/api/events</code>.</p>
"""


class ControllerHolder:
    """<summary>
    The controller comes and goes with the USB connection; this is the stable
    handle.
    </summary>
    <remarks>
    The server outlives any one controller: an unplug destroys the controller
    and a replug builds a new one, while the same server keeps listening on
    the same port. Holding the controller directly would therefore pin a dead
    one, so everything here reads ``holder.controller`` fresh at the moment it
    is needed and copes with None.

    The attribute is written by the run loop and read by request threads with
    no lock. That is deliberate and safe: a single reference assignment is
    atomic, and a reader that catches the old controller a moment late is
    handled by the None check and the ``is not`` comparison in the event
    stream.
    </remarks>
    """

    def __init__(self) -> None:
        """
        <summary>
        Start with no controller attached.
        </summary>
        """
        self.controller = None


class ApiServer(ThreadingHTTPServer):
    """<summary>
    The listening socket and everything a request handler needs to answer.
    </summary>
    <remarks>
    Threaded because the event stream holds a connection open indefinitely: a
    single threaded server would be blocked by the first page that opened one
    and answer nothing else. The threads are daemon threads and
    ``block_on_close`` is off, so shutdown does not wait on a stream that is
    parked waiting for the next key press.

    The handler reads its settings, its allowed origins and the controller
    holder off this object, so anything a request needs is put here at
    construction and never mutated afterwards. The exception is
    ``config_lock``, which serialises the read and the atomic replace of the
    config file so two page saves at once cannot interleave.

    The allowed origins are worked out from the port actually bound, not the
    port asked for. Port 0 means the operating system chooses, and the set
    would otherwise name a port nothing is listening on, which would refuse
    every request the page made.
    </remarks>
    """

    daemon_threads = True
    # On Linux SO_REUSEADDR only skips the TIME_WAIT delay after a restart. On
    # Windows it lets a second daemon bind the same port while the first is
    # still listening, and requests then go to whichever one wins, so leave it
    # off there and let the second daemon fail loudly instead.
    allow_reuse_address = sys.platform != "win32"
    block_on_close = False  # never wait on an event stream thread at shutdown

    def __init__(self, settings: ServerSettings, holder: ControllerHolder,
                 log: Callable[[str], None] = print, web_dir: Path = WEB_DIR) -> None:
        """<summary>
        Bind the socket and record what the handlers will need.
        </summary>
        <param name="settings">Bind address, port and token from the config.
        The bind decides whether the loopback Host check applies.</param>
        <param name="holder">The shared handle to whatever controller is
        alive, filled in by the run loop.</param>
        <param name="log">Where request faults are reported.</param>
        <param name="web_dir">Root of the built configuration page. Static
        files are confined to this folder and nothing above it.</param>
        <remarks>
        Binding happens in the base constructor, so this raises before
        anything else is set up rather than starting a half built server.
        Nothing is served until <see cref="start"/> is called.
        </remarks>
        
        <exception cref="OSError">The port is already taken, usually a second
        daemon.</exception>"""
        super().__init__((settings.bind, settings.port), ApiHandler)
        self.settings = settings
        self.holder = holder
        self.log = log
        self.web_dir = web_dir
        self.thread: threading.Thread | None = None
        self.stopping = threading.Event()
        self.config_lock = threading.Lock()
        port = self.server_address[1]
        self.allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}

    @property
    def port(self) -> int:
        """<summary>
        The port actually bound, which is not always the one asked for.
        </summary>
        <returns>The live port number.</returns>
        <remarks>
        A configured port of 0 means the operating system picks one, so this
        is what to print and what the tests connect to. Reading
        ``settings.port`` instead would give 0.
        </remarks>
        """
        return self.server_address[1]

    def handle_error(self, request, client_address) -> None:
        """<summary>
        A browser dropping a connection is normal, not a traceback.
        </summary>
        <param name="request">The socket the fault happened on, unused.</param>
        <param name="client_address">Who was connected, named in the log
        line.</param>
        <remarks>
        Closing a tab or navigating away kills the event stream mid write,
        and the base class prints a full traceback for each one. That noise
        buries the controller's log and reads as a fault when nothing is
        wrong, so the four connection errors are swallowed and everything
        else is reported as a single line.

        The exception is fetched from ``sys.exc_info`` rather than passed in,
        because that is the interface the base class calls this through.
        </remarks>
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        self.log(f"api request from {client_address[0]} failed: {exc!r}")

    def start(self) -> "ApiServer":
        """<summary>
        Begin serving on a background thread.
        </summary>
        <returns>This same server, so the caller can start and keep it in one
        line.</returns>
        <remarks>
        Returns as soon as the thread is running, not when the first request
        arrives. The socket was already bound by the constructor, so a page
        that connects the instant this returns is queued rather than refused.

        A daemon thread, so a daemon killed without <see cref="stop"/> still
        exits rather than hanging on the listener.
        </remarks>
        """
        self.thread = threading.Thread(target=self.serve_forever, name="deckplate-api", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        """<summary>
        Stop serving and release the port.
        </summary>
        <remarks>
        The order is the point. The ``stopping`` event is set first so every
        open event stream sees it and unwinds within its quarter second poll;
        only then is the accept loop shut down and the socket closed. Closing
        first would leave those streams writing to a dead socket.

        Safe to call on a server that was never started, which is what the
        run loop's ``finally`` does when the API failed to bind.
        </remarks>
        """
        self.stopping.set()
        self.shutdown()
        self.server_close()


def start(settings: ServerSettings, holder: ControllerHolder, log: Callable[[str], None] = print) -> ApiServer:
    """<summary>
    Build a server and start it, in one call.
    </summary>
    <param name="settings">Bind address, port and token from the config.</param>
    <param name="holder">The shared handle the run loop fills in.</param>
    <param name="log">Where request faults are reported.</param>
    <returns>The running server. The caller must stop it.</returns>
    <remarks>
    The convenience door for the command line. It does not check
    ``settings.enabled``: whoever calls this has already decided.
    </remarks>
    
    <exception cref="OSError">The port is already taken. The run loop catches
    this and carries on without an API rather than refusing to drive the
    deck.</exception>"""
    return ApiServer(settings, holder, log).start()


def _safe_image_name(name: str) -> str | None:
    """<summary>
    A plain file name for the images folder, or None if it is anything else.
    </summary>
    <param name="name">The name as it arrived, from a URL path or the
    X-Filename header. Already URL decoded.</param>
    <returns>The trimmed name when it is safe to join to the images folder,
    None otherwise.</returns>
    <remarks>
    This is the only thing standing between a request and the rest of the
    disk, so it allows rather than forbids: a short list of permitted
    characters, no separator of either kind, no bare dot or double dot, and
    no leading dot. Anything unlisted is refused, which is why an unusual but
    harmless name is rejected rather than escaped.

    Returning None must always become a 404 and never a fallback name. The
    length cap is there because some filesystems refuse long names and the
    error would surface as a server fault rather than a bad request.
    </remarks>
    """
    name = name.strip()
    if not name or "/" in name or "\\" in name or name in (".", "..") or len(name) > 120:
        return None
    if not all(ch.isalnum() or ch in "._- ()" for ch in name):
        return None
    if name.startswith("."):
        return None
    return name


def _hostname(host_header: str) -> str:
    """<summary>
    The host out of a Host header, with any port and any brackets removed.
    </summary>
    <param name="host_header">The raw header value, or an empty string when
    there was none.</param>
    <returns>The lowercased host alone: 'localhost:8765' gives 'localhost'
    and '[::1]:8765' gives '::1'.</returns>
    <remarks>
    The IPv6 form is the trap. Splitting on the last colon first would cut an
    address like ::1 in half and the loopback check would then reject the
    page's own requests, so the brackets are handled before the port is.

    Lowercased because the check that follows compares against a fixed set,
    and a Host header's case is not guaranteed.
    </remarks>
    """
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if ":" in host else host


class ApiHandler(BaseHTTPRequestHandler):
    """<summary>
    One request, from the access checks through to the reply.
    </summary>
    <remarks>
    A fresh instance per connection, on its own thread, so anything stored on
    ``self`` lasts for that connection only. ``self.body`` is set by
    <see cref="_gate"/> and read by the route methods: a route that runs
    without the gate having run first would see no body at all, which is why
    every verb method calls the gate before it dispatches.

    HTTP/1.1 with keep alive, which is what makes the page's many small tile
    requests cheap. It also means every reply must carry an accurate
    Content-Length or the connection desynchronises and the next request on
    it is misread; <see cref="_send"/> is the only correct way to answer.

    The route methods never touch the deck. They read the controller's
    snapshot and caches, or queue work with ``submit`` for the controller
    thread, and return at once. Blocking here would stall the page and,
    worse, invite a second thread onto the USB device.
    </remarks>
    """

    server: ApiServer
    protocol_version = "HTTP/1.1"

    # Plumbing

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        """<summary>
        Swallow the base class's per request logging.
        </summary>
        <remarks>
        The page polls tiles constantly, so one line per request would bury
        the controller's log entirely. Faults are still reported, through
        <see cref="ApiServer.handle_error"/>, which is the only API logging
        there is.
        </remarks>
        """
        pass  # one line per request is noise next to the controller's log

    def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        """<summary>
        Write one complete reply: status, headers and body.
        </summary>
        <param name="body">The bytes to send. May be empty but not None.</param>
        <param name="content_type">The full Content-Type, charset included
        where it matters.</param>
        <param name="status">The status code, 200 unless given.</param>
        <remarks>
        Every reply on this server goes through here, and must, because the
        Content-Length it sets is what keeps a keep alive connection in step.
        Writing to ``wfile`` directly would leave the next request on that
        connection unreadable.

        No-store on everything. Tiles change with every redraw and the config
        changes as it is edited, so a cached copy is always the wrong answer
        and the page would show stale state with no way to tell.

        No CORS headers are sent, here or anywhere: that absence is part of
        the defence and not an oversight.
        </remarks>
        """
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data, status: HTTPStatus = HTTPStatus.OK) -> None:
        """<summary>
        Reply with a JSON body.
        </summary>
        <param name="data">Anything json.dumps will take. Errors are sent as
        {"error": "..."} and successes usually as {"ok": true}.</param>
        <param name="status">The status code, 200 unless given.</param>
        <remarks>
        Error text here is read by a person through the page, so it says what
        was wrong with the request in plain words. It must never carry a file
        system path from outside the config folder, or anything from the
        environment, since the page can be open to whoever can reach the port.
        </remarks>
        """
        self._send(json.dumps(data).encode("utf-8"), "application/json; charset=utf-8", status)

    def _gate(self, body_type: str | tuple[str, ...] | None) -> bool:
        """<summary>
        Read the body and run the access checks. False if a response was
        already sent.
        </summary>
        <param name="body_type">The content type, or types, this route will
        accept. None for routes with no body, which skips that check only.</param>
        <returns>True when the request may proceed and ``self.body`` holds
        its bytes. False when it was refused and the reply has gone.</returns>
        <remarks>
        Every verb method must call this first and return immediately on
        False. It is the single place the access rules live, so a route that
        dispatches without it is unprotected, and a route reached without it
        would also see an empty body.

        The body is always consumed first, before any check can refuse the
        request, so a rejected request never leaves its bytes in the stream
        to be misread as the next request on a keep alive connection. The two
        length faults set ``close_connection`` for the same reason: with a
        length that could not be trusted there is no way to know where the
        body ended, so the connection is the only safe thing to discard.

        Then the three defences, in order. An Origin that is present and not
        this server is refused; an absent Origin is allowed, because browsers
        always send one and command line clients do not. On a loopback bind a
        Host that is not a loopback name is refused, which is what a request
        arriving through a rewriting proxy looks like. Last, the content type
        must be one this route named, which is what stops a cross origin HTML
        form: a form can only send three types and none of them is JSON or
        TOML, and no preflight is ever granted to let it send another.

        The token, when set, is checked against both the X-Token header and a
        token query parameter. The query form exists so an image or event
        stream URL can carry it, and is the weaker of the two.

        Order matters for what is disclosed as well as for correctness: a
        request from a disallowed origin is refused before the token is
        looked at, so nothing here confirms whether a token was right.
        </remarks>
        """
        self.body = b""
        raw_length = self.headers.get("Content-Length")
        if raw_length:
            try:
                length = int(raw_length)
                if length < 0:
                    raise ValueError
            except ValueError:
                self.close_connection = True
                self._send_json({"error": "bad Content-Length"}, HTTPStatus.BAD_REQUEST)
                return False
            if length > MAX_BODY_BYTES:
                self.close_connection = True
                self._send_json({"error": "body too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return False
            self.body = self.rfile.read(length)

        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in self.server.allowed_origins:
            self._send_json({"error": "origin not allowed"}, HTTPStatus.FORBIDDEN)
            return False
        if self.server.settings.bind in LOOPBACK_ADDRESSES:
            if _hostname(self.headers.get("Host") or "") not in LOOPBACK_ADDRESSES:
                self._send_json({"error": "host not allowed"}, HTTPStatus.FORBIDDEN)
                return False
        token = self.server.settings.token
        if token:
            query = parse_qs(urlparse(self.path).query)
            if self.headers.get("X-Token") != token and query.get("token", [None])[0] != token:
                self._send_json({"error": "token required"}, HTTPStatus.UNAUTHORIZED)
                return False
        if body_type is not None:
            sent = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            allowed = (body_type,) if isinstance(body_type, str) else body_type
            if sent not in allowed:
                self._send_json({"error": f"Content-Type must be {' or '.join(allowed)}"},
                                HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return False
        return True

    def _controller(self):
        """<summary>
        The live controller, or None with a 503 already sent.
        </summary>
        <returns>The controller when a deck is connected, otherwise None.</returns>
        <remarks>
        The standard opening of every route that needs the deck, and the
        reason they all read ``if controller is None: return``. Forgetting
        that return sends a second reply on top of the 503 and corrupts the
        connection.

        503 rather than 404 because "no deck plugged in" is temporary and the
        page is expected to try again. The event stream deliberately does not
        use this: a 503 there would make the browser abandon the stream for
        good.
        </remarks>
        """
        controller = self.server.holder.controller
        if controller is None:
            self._send_json({"error": "deck not connected"}, HTTPStatus.SERVICE_UNAVAILABLE)
        return controller

    def _read_json(self) -> dict | None:
        """<summary>
        Decode the body as a JSON object, or answer 400 and give None.
        </summary>
        <returns>The decoded object, or None when a reply has already been
        sent.</returns>
        <remarks>
        An empty body is read as an empty object, which is what a POST with
        nothing to say sends. A top level list or string is refused: every
        route here expects named fields, and accepting a bare value would
        push the type error down into the route as a crash instead of a 400.

        Requires <see cref="_gate"/> to have run, since the body is read from
        the attribute the gate fills in. The content type was already checked
        there, so this only has to deal with malformed JSON.
        </remarks>
        """
        try:
            data = json.loads(self.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "body is not valid JSON"}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(data, dict):
            self._send_json({"error": "body must be a JSON object"}, HTTPStatus.BAD_REQUEST)
            return None
        return data

    # Verbs

    def do_GET(self) -> None:
        """<summary>
        Route every read: the page itself, state, tiles, config, images,
        themes, geocoding and the event stream.
        </summary>
        <remarks>
        The gate runs with no body type, because a GET has no body to check.
        The Origin, Host and token checks still apply in full, so a website
        cannot read the config or the state either.

        Prefixes are matched after the exact paths they could shadow, so the
        order of these tests is part of the routing and not tidiness. The
        query string is stripped before matching: routes are decided by path
        alone, and ``?scale=`` or ``?token=`` must not change which one runs.
        </remarks>
        """
        if not self._gate(None):
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._static("index.html", PLACEHOLDER)
        if path.startswith("/static/"):
            return self._static(unquote(path[len("/static/"):]), None)
        if path == "/api/state":
            return self._state()
        if path.startswith("/api/tiles/"):
            return self._tile(path)
        if path == "/api/config":
            return self._config_get()
        if path == "/api/document":
            return self._document_get()
        if path == "/api/images":
            return self._images_list()
        if path.startswith("/api/images/"):
            return self._image_get(unquote(path[len("/api/images/"):]))
        if path == "/api/themes":
            return self._themes_list()
        if path.startswith("/api/themes/"):
            return self._theme_icon(unquote(path[len("/api/themes/"):]))
        if path == "/api/geocode":
            return self._geocode()
        if path == "/api/events":
            return self._events()
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:
        """<summary>
        Route the two config writes: JSON for the form, TOML for the raw
        file.
        </summary>
        <remarks>
        The path is looked at before the gate here, unlike the other verbs,
        because the two routes accept different content types and the gate
        has to be told which. That is the whole reason for the shape: the
        document route is gated for JSON and returns, and everything else
        falls through to the TOML gate.

        Both routes validate before they write and both write atomically, so
        a config that would not parse never reaches the disk and a reader
        never sees a half written file.
        </remarks>
        """
        path = urlparse(self.path).path
        if path == "/api/document":
            if self._gate(JSON_TYPE):
                self._document_put()
            return
        if not self._gate(TOML_TYPES):
            return
        if path == "/api/config":
            return self._config_put()
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        """<summary>
        Route the actions: picture upload, page change, brightness, a
        simulated press, wake and sleep.
        </summary>
        <remarks>
        Image upload is gated separately because its body is the picture
        itself and its content type is one of the image types, not JSON.
        Every other action takes JSON, which is what keeps a cross origin
        HTML form from reaching them: a form cannot send that content type
        without a preflight, and none is granted.

        The upload list is the image types this server will store, so the
        types it is willing to receive and the extensions it maps them to
        stay the same list.

        Nothing here talks to the deck. Each action is queued with ``submit``
        and the reply goes back at once, so a slow redraw never holds the
        connection open.
        </remarks>
        """
        path = urlparse(self.path).path
        if path == "/api/images":
            if self._gate(tuple(IMAGE_TYPES)):
                self._image_upload()
            return
        if not self._gate(JSON_TYPE):
            return
        handlers = {"/api/page": self._page, "/api/brightness": self._brightness,
                    "/api/press": self._press, "/api/wake": self._wake, "/api/sleep": self._sleep}
        handler = handlers.get(path)
        if handler is None:
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        handler()

    def do_DELETE(self) -> None:
        """<summary>
        Route the one deletion: a picture in the images folder.
        </summary>
        <remarks>
        Gated for JSON even though the body is empty and unread. A browser
        cannot send DELETE from a form at all, and requiring the content type
        keeps this in step with the other writing routes rather than being
        the one exception someone later has to reason about.

        The route itself refuses to remove a picture anything still uses, so
        nothing on the deck can be broken by a tidy up.
        </remarks>
        """
        if not self._gate(JSON_TYPE):
            return
        path = urlparse(self.path).path
        if path.startswith("/api/images/"):
            return self._image_delete(unquote(path[len("/api/images/"):]))
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:
        """<summary>
        Answer a preflight with a refusal and no CORS headers at all.
        </summary>
        <remarks>
        This is a defence, not a stub. Answering a preflight with
        Access-Control-Allow-Origin would let any website on the internet
        send JSON to this server and drive the deck, so the reply carries no
        CORS headers of any kind and the browser refuses the real request on
        the page's behalf. Nothing may be added here for convenience.

        The gate still runs, so the refusal is consistent with every other
        route. Its result is discarded because either answer ends the request
        the same way.
        </remarks>
        """
        # No CORS headers on purpose: a cross origin preflight must fail.
        self._gate(None) and self._send_json({"error": "not allowed"}, HTTPStatus.METHOD_NOT_ALLOWED)

    # Routes

    def _static(self, name: str, fallback: str | None) -> None:
        """<summary>
        Serve one built file of the configuration page.
        </summary>
        <param name="name">Path relative to the web folder, already URL
        decoded.</param>
        <param name="fallback">HTML to send when the file is missing, or None
        to answer 404. Only the index has one.</param>
        <remarks>
        The containment check is the important line. Both sides are resolved
        before they are compared, so a name that climbs out of the web folder
        with dots, or through a symbolic link, lands outside it and is
        refused as a 404 rather than served. Comparing the unresolved paths
        would miss both.

        Anything unrecognised is sent as an octet stream rather than guessed
        at, which makes a browser save it instead of running it.

        The fallback exists so a source checkout with no built page still
        answers something useful at the root instead of a bare 404.
        </remarks>
        """
        file = (self.server.web_dir / name)
        try:
            file.resolve().relative_to(self.server.web_dir.resolve())
        except ValueError:
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        if file.is_file():
            types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                     ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
                     ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2"}
            return self._send(file.read_bytes(), types.get(file.suffix, "application/octet-stream"))
        if fallback is not None:
            return self._send(fallback.encode("utf-8"), "text/html; charset=utf-8")
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _state(self) -> None:
        """<summary>
        GET /api/state: the controller's snapshot as JSON.
        </summary>
        <remarks>
        The page's starting picture of everything: pages, the current page,
        brightness, whether the deck is asleep. The event stream carries the
        changes afterwards, so this is fetched once rather than polled.

        The snapshot is taken by the controller and is a plain copy, so
        nothing here holds a reference into live state or blocks the
        controller thread while the reply is written.
        </remarks>
        """
        controller = self._controller()
        if controller is not None:
            self._send_json(controller.snapshot())

    def _tile_request(self, path: str) -> tuple[int, int, int, bool] | None:
        """<summary>
        Parse a tile route: position, scale and whether the loop was asked
        for.
        </summary>
        <param name="path">The request path, without the query string.</param>
        <returns>(row, column, scale, animated), or None after an error
        response has been sent.</returns>
        <remarks>
        Every number is checked against the real grid and the real scale
        ceiling before it reaches the controller, because scale multiplies
        the drawing work and an unbounded one is a way to make the daemon
        spend the whole machine on one request.

        Blank query values are kept rather than dropped, so a page bug that
        sends "?scale=" is refused loudly instead of quietly falling back to
        the deck's own size and looking like it worked.

        The .gif and .jpg suffixes are both removed before the position is
        read, so the same parse serves the still tile and the animation.
        </remarks>
        """
        animated = path.endswith(".gif")
        parts = path[len("/api/tiles/"):].removesuffix(".gif").removesuffix(".jpg").split("/")
        try:
            row, column = int(parts[0]), int(parts[1])
            if not (0 <= row < layout.ROWS and 0 <= column < layout.COLUMNS):
                raise ValueError
        except (ValueError, IndexError):
            self._send_json({"error": "bad tile position"}, HTTPStatus.BAD_REQUEST)
            return None
        # keep_blank_values so "?scale=" is seen and refused, rather than
        # silently falling back to the deck's own size when the page has a bug.
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        try:
            scale = int(query.get("scale", ["1"])[0])
        except ValueError:
            self._send_json({"error": "scale must be a whole number"}, HTTPStatus.BAD_REQUEST)
            return None
        if not 1 <= scale <= MAX_PREVIEW_SCALE:
            self._send_json({"error": f"scale must be 1 to {MAX_PREVIEW_SCALE}"}, HTTPStatus.BAD_REQUEST)
            return None
        return row, column, scale, animated

    def _tile(self, path: str) -> None:
        """<summary>
        GET /api/tiles/row/col.jpg or .gif: what that panel is showing.
        </summary>
        <param name="path">The request path, without the query string.</param>
        <remarks>
        Drawn for the page and only for the page. Both preview calls render
        into memory and send nothing to the deck, at any scale, which is why
        a scale far larger than the hardware image size is allowed at all:
        the page shows the panels much bigger than the deck does.

        A still panel asked for as a .gif is a 404 rather than a single frame
        picture, so the page can tell animated panels from still ones by
        asking, without a second route to list them.

        The JPEG quality is fixed at 90 and the image is converted to RGB
        first, because a palette or alpha image cannot be saved as JPEG at
        all and would raise here rather than answer.
        </remarks>
        """
        controller = self._controller()
        if controller is None:
            return
        parsed = self._tile_request(path)
        if parsed is None:
            return
        row, column, scale, animated = parsed
        if animated:
            # The loop of an animated panel. Drawn and encoded for the page
            # only; preview_animation sends nothing to the deck.
            encoded = controller.preview_animation(row, column, scale)
            if encoded is None:
                return self._send_json({"error": "that panel is not animated"}, HTTPStatus.NOT_FOUND)
            return self._send(encoded, "image/gif")
        # Drawn for the page only. preview_tile sends nothing to the deck.
        image = controller.preview_tile(row, column, scale)
        if image is None:
            return self._send_json({"error": "no tile yet"}, HTTPStatus.NOT_FOUND)
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=90)
        self._send(out.getvalue(), "image/jpeg")

    def _config_get(self) -> None:
        """<summary>
        GET /api/config: the config file exactly as it is on disk.
        </summary>
        <remarks>
        Sent as bytes, not as text that has been read and re-encoded, so
        comments, ordering and line endings all survive the round trip to the
        page's text editor and back.

        Read under the config lock, so a save happening at the same moment
        cannot be caught half done. A config that has no file behind it,
        which the tests build, is a 404 rather than an empty body.
        </remarks>
        """
        controller = self._controller()
        if controller is None:
            return
        path = controller.config.path
        if path is None:
            return self._send_json({"error": "config has no file"}, HTTPStatus.NOT_FOUND)
        with self.server.config_lock:
            data = Path(path).read_bytes()
        self._send(data, "application/toml; charset=utf-8")

    def _config_put(self) -> None:
        """<summary>
        PUT /api/config: replace the config file with the text sent.
        </summary>
        <remarks>
        A whole file replacement, not a merge: whatever is sent becomes the
        config. The page reads the file, edits it and puts it back, so
        anything it did not send is gone on purpose.

        UTF-8 only. A body that is not valid UTF-8 is refused before the
        parse, because the error from a decode failure halfway through a TOML
        parse says nothing useful about what to fix.
        </remarks>
        <see cref="_write_config_text"/>
        """
        controller = self._controller()
        if controller is None:
            return
        try:
            text = self.body.decode("utf-8")
        except UnicodeDecodeError:
            return self._send_json({"error": "config must be UTF-8 text"}, HTTPStatus.BAD_REQUEST)
        # Bytes, not text, so line endings survive on Windows; and written to a
        # temporary file then renamed, so nobody ever reads a half written config.
        if self._write_config_text(controller, text):
            self._send_json({"ok": True})

    def _write_config_text(self, controller, text: str) -> bool:
        """<summary>
        Validate, then write byte exact and atomically. False if a response
        was sent.
        </summary>
        <param name="controller">The live controller, whose config supplies
        the path and the base folder the file is validated against.</param>
        <param name="text">The complete new config file.</param>
        <returns>True when it was written and a reload queued, False when a
        reply has already gone.</returns>
        <remarks>
        The order is the safety. Nothing is written until the text has parsed
        cleanly against the same base folder the daemon will use, so a config
        that would stop the daemon starting can never reach the disk through
        the page.

        Written as bytes to a temporary file beside the real one and then
        renamed, which is atomic on both platforms. Nobody ever reads a half
        written config, and line endings survive on Windows because nothing
        translates them.

        Held under the config lock across both steps, so two saves at once
        cannot interleave their write and rename. The reload is queued for
        the controller thread rather than done here: applying a config touches
        the deck, and only that thread may.
        </remarks>
        """
        path = controller.config.path
        if path is None:
            self._send_json({"error": "config has no file"}, HTTPStatus.NOT_FOUND)
            return False
        try:
            parse(text, controller.config.base_dir, Path(path))
        except ConfigError as err:
            self._send_json({"error": str(err)}, HTTPStatus.BAD_REQUEST)
            return False
        path = Path(path)
        temporary = path.with_name(path.name + ".tmp")
        with self.server.config_lock:
            temporary.write_bytes(text.encode("utf-8"))
            os.replace(temporary, path)
        controller.submit(controller.reload_from_disk)
        return True

    def _document_get(self) -> None:
        """<summary>
        GET /api/document: the config as JSON, the shape the page's form
        edits.
        </summary>
        <remarks>
        A view of the same config the TOML route serves, converted to named
        fields with the defaults filled in, so the form has a value for every
        control and does not have to know what the daemon would assume.

        Comments and ordering do not survive this form. Anyone who wants them
        kept edits the TOML through the other route.
        </remarks>
        """
        controller = self._controller()
        if controller is not None:
            self._send_json(document_from_config(controller.config))

    def _document_put(self) -> None:
        """<summary>
        PUT /api/document: rewrite the config file from the form's JSON.
        </summary>
        <remarks>
        The document is turned into TOML first and then goes through the same
        validation and atomic write as a raw config save, so both routes have
        one set of rules and one way of failing. There is no second code path
        that writes a config.

        This rewrites the whole file, so comments and any hand made ordering
        in the existing config are lost. That is the accepted cost of editing
        through the form.
        </remarks>
        <see cref="_write_config_text"/>
        """
        controller = self._controller()
        if controller is None:
            return
        document = self._read_json()
        if document is None:
            return
        try:
            text = to_toml(document)
        except ConfigError as err:
            return self._send_json({"error": str(err)}, HTTPStatus.BAD_REQUEST)
        if self._write_config_text(controller, text):
            self._send_json({"ok": True})

    def _images_dir(self, controller) -> Path:
        """
        <summary>
        The images folder beside the running configuration.
        </summary>
        <param name="controller">The controller whose config is in use.</param>
        <returns>A Path.</returns>
        """
        return Path(controller.config.base_dir) / "images"

    def _image_users(self, controller, folder: Path) -> dict[str, list[str]]:
        """<summary>
        Which keys, strip panels and page wallpapers use each picture in the
        folder.
        </summary>
        <param name="controller">The live controller, for the config it is
        running.</param>
        <param name="folder">The images folder every path is measured
        against.</param>
        <returns>File name to a list of plain descriptions of where it is
        used. A picture nothing uses is simply absent.</returns>
        <remarks>
        Paths are resolved and made relative to the folder, so a config that
        names the same picture by a different route still counts as using it.
        Anything that resolves outside the folder is skipped rather than
        raising: a config may point at a picture anywhere on disk, and only
        the ones inside the folder can be listed or deleted here.

        The descriptions are for a person to read in the page, so rows and
        columns are numbered from one, not from zero as the config is.

        The whole config is walked for every call. It is a handful of pages,
        and a stale cache showing the wrong answer here would let a picture
        still in use be deleted.
        </remarks>
        """
        users: dict[str, list[str]] = {}

        def note(path, where):
            """<summary>
            Record that ``where`` refers to an image, if that image really
            lives inside the images folder.
            </summary>
            <param name="path">Path named by a page, config or document.</param>
            <param name="where">Human readable place the reference came from.</param>
            <remarks>
            Anything that resolves outside the folder is dropped in silence
            rather than reported. That is deliberate: this builds the list of
            which images are in use, and a path escaping the folder is not an
            image of ours to count. The resolve happens before the comparison
            so a symlink cannot smuggle an outside file into the list.
            </remarks>
            """
            try:
                name = Path(path).resolve().relative_to(folder.resolve()).as_posix()
            except (ValueError, OSError):
                return
            users.setdefault(name, []).append(where)

        for page in controller.config.pages:
            if page.wallpaper is not None:
                note(page.wallpaper, f"{page.name}: wallpaper")
            for (row, column), key in page.keys.items():
                if key.image is not None:
                    note(key.image, f"{page.name}: row {row + 1}, column {column + 1}")
        for row, tile in enumerate(controller.config.strip):
            if tile.image is not None:
                note(tile.image, f"strip panel {row + 1}")
        return users

    def _images_list(self) -> None:
        """<summary>
        GET /api/images: the pictures in the images folder, with what uses
        each.
        </summary>
        <remarks>
        A missing folder is an empty list, not an error. It has simply never
        been used yet, and the upload route creates it.

        The folder path is returned so the page can tell someone where to put
        pictures by hand. It is a path on the machine running the daemon,
        which is the same machine as the page on a loopback bind; off
        loopback it is of no use to the reader and is still sent, so widening
        the bind is a decision to make deliberately.

        ``used_by`` is what makes the delete refusal understandable before it
        happens, so the page can grey out a picture rather than offer a
        deletion that will fail.
        </remarks>
        """
        controller = self._controller()
        if controller is None:
            return
        folder = self._images_dir(controller)
        files = sorted(p for p in folder.iterdir()
                       if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES) if folder.is_dir() else []
        users = self._image_users(controller, folder)
        self._send_json({
            "folder": str(folder),
            "images": [p.name for p in files],
            "details": [{"name": p.name, "bytes": p.stat().st_size, "used_by": users.get(p.name, [])} for p in files],
        })

    def _image_delete(self, name: str) -> None:
        """<summary>
        DELETE /api/images/name: remove a picture that nothing uses.
        </summary>
        <param name="name">The file name from the URL, already decoded and
        not yet trusted.</param>
        <remarks>
        Three gates before anything goes. The name has to pass
        <see cref="_safe_image_name"/>, so nothing outside the images folder
        can be reached; the file has to exist; and nothing in the config may
        still refer to it, which is answered with a 409 naming every user so
        the refusal can be acted on.

        An unsafe name and a missing file give the same 404 on purpose:
        there is no reason to tell a caller which of the two it was.

        This is the only route in the server that removes anything, and it is
        deliberately the narrowest.
        </remarks>
        """
        controller = self._controller()
        if controller is None:
            return
        safe = _safe_image_name(name)
        folder = self._images_dir(controller)
        file = folder / safe if safe else None
        if file is None or not file.is_file():
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        used_by = self._image_users(controller, folder).get(safe, [])
        if used_by:
            return self._send_json({"error": "still used by " + ", ".join(used_by), "used_by": used_by},
                                   HTTPStatus.CONFLICT)
        try:
            file.unlink()
        except OSError as err:
            return self._send_json({"error": f"could not delete: {err}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        self._send_json({"ok": True, "name": safe})

    def _image_get(self, name: str) -> None:
        """<summary>
        GET /api/images/name: one picture out of the images folder.
        </summary>
        <param name="name">The file name from the URL, already decoded and
        not yet trusted.</param>
        <remarks>
        The name check is what confines this to the images folder, so it runs
        before the path is built and its failure is a 404. Without it this
        route would read any file the daemon can read.

        The content type comes from the extension, and an unknown one is sent
        as an octet stream rather than guessed, so a browser saves it instead
        of rendering it. The file is not sniffed: only what the folder is
        for is served from it.
        </remarks>
        """
        controller = self._controller()
        if controller is None:
            return
        safe = _safe_image_name(name)
        file = self._images_dir(controller) / safe if safe else None
        if file is None or not file.is_file():
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
        self._send(file.read_bytes(), types.get(file.suffix.lower(), "application/octet-stream"))

    def _themes_list(self) -> None:
        """<summary>
        GET /api/themes: the icon themes shipped with the package.
        </summary>
        <remarks>
        Independent of the deck, so no controller is needed and this answers
        while nothing is plugged in. The page can therefore show its icon
        picker between decks instead of going blank.

        The themes live inside the installed package, not in the config
        folder, so nothing here can be added to or removed through the API.
        </remarks>
        """
        self._send_json({"themes": themes.list_themes()})

    def _theme_icon(self, rest: str) -> None:
        """<summary>
        GET /api/themes/slug/icon.png: one icon out of a shipped theme.
        </summary>
        <param name="rest">The part of the path after the prefix, in the form
        ``slug/icon.png``. Already decoded and not yet trusted.</param>
        <remarks>
        Nothing is joined to a folder here. The slug and the icon name are
        handed to the themes module, which looks them up against what it
        actually ships and answers None for anything it does not recognise,
        so a name that tries to climb out simply does not match.

        A missing slug, a missing suffix and an unknown icon are all the same
        404: the page has a list of what exists and has no need to be told
        which part it got wrong.
        </remarks>
        """
        slug, sep, icon_png = rest.partition("/")
        if not sep or not icon_png.endswith(".png"):
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        file = themes.icon_path(slug, icon_png[:-len(".png")])
        if file is None:
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        self._send(file.read_bytes(), "image/png")

    def _image_upload(self) -> None:
        """<summary>
        POST /api/images: store an uploaded picture in the images folder.
        </summary>
        <remarks>
        The body is the file itself, raw, with the name in the X-Filename
        header. No multipart form parsing, which keeps this route small and
        keeps the content type one a cross origin form could never send.

        The name must pass <see cref="_safe_image_name"/>, which is what
        confines the write to the images folder. When it carries no
        recognised extension one is appended from the content type, and the
        gate has already limited that to the image types, so the stored name
        always ends in something the reading route will serve.

        An existing file of the same name is overwritten without asking. That
        is how a picture is updated in place, and it is the one destructive
        thing this route does.

        The size is bounded by the gate's body limit, not by anything here.
        </remarks>
        """
        controller = self._controller()
        if controller is None:
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        requested = self.headers.get("X-Filename") or ""
        safe = _safe_image_name(requested)
        if not safe:
            return self._send_json({"error": "X-Filename must be a plain image file name"}, HTTPStatus.BAD_REQUEST)
        if Path(safe).suffix.lower() not in IMAGE_SUFFIXES:
            safe = Path(safe).stem + IMAGE_TYPES[content_type]
        if not self.body:
            return self._send_json({"error": "empty file"}, HTTPStatus.BAD_REQUEST)
        folder = self._images_dir(controller)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / safe).write_bytes(self.body)
        self._send_json({"ok": True, "name": safe, "path": f"images/{safe}"})

    def _geocode(self) -> None:
        """<summary>
        GET /api/geocode?name=Town: coordinates for a place, for the weather
        tile.
        </summary>
        <remarks>
        The only route that reaches the network, and the only reason the
        daemon needs one at all. It is called when someone types a place name
        into the page, never on a timer, so a machine with no network is
        perfectly usable until then.

        Every exception is caught and reported as a 502 rather than allowed
        out. Network trouble is the expected case here, it is the remote
        service failing and not this server, and a traceback per typed
        character would be unreadable.

        The reply is rebuilt field by field rather than passed straight
        through, so only the five fields the page needs are ever forwarded
        from the remote service.
        </remarks>
        """
        from . import weather

        name = parse_qs(urlparse(self.path).query).get("name", [""])[0].strip()
        if not name:
            return self._send_json({"error": "'name' is required"}, HTTPStatus.BAD_REQUEST)
        try:
            places = weather.geocode(name)
        except Exception as err:  # network trouble is reported, not raised
            return self._send_json({"error": f"lookup failed: {err}"}, HTTPStatus.BAD_GATEWAY)
        self._send_json({"places": [
            {"name": p.name, "region": p.region, "country": p.country,
             "latitude": p.latitude, "longitude": p.longitude} for p in places]})

    def _events(self) -> None:
        """<summary>
        GET /api/events: the server sent events stream the page lives on.
        </summary>
        <remarks>
        Holds one connection open for as long as the page is there, and
        pushes key presses, page changes and tile updates as they happen, so
        nothing has to be polled.

        Always a 200, even with no deck connected, and that is the whole
        design of this route. A 503 would make the browser's EventSource give
        up for good and the page would never update again after a replug, so
        instead the stream waits, says it is waiting, and picks up the new
        controller when the deck returns, sending a fresh hello with no
        reconnect on the page.

        The connection is marked for closing rather than keep alive, because
        the body never ends and there can be no Content-Length.

        A subscription must always be unsubscribed, which is why the inner
        loop is wrapped in its own try and finally: a page that closes mid
        stream would otherwise leave the controller publishing into a queue
        nobody reads.

        Every write can fail at any moment when the far end has gone, so the
        broken connection errors are swallowed at the end and the thread
        simply finishes. That is a closed tab, not a fault.
        </remarks>
        <see cref="ApiServer.stop"/>
        """
        # Always a 200 event stream, even with no deck connected. A 503 here
        # would make the browser's EventSource give up for good, so the page
        # would never update again after an unplug and replug. Instead the one
        # connection is held open across disconnects: it waits for a controller,
        # streams from it, and when the deck comes back as a new controller it
        # picks that up and sends a fresh hello, with no reconnect on the page.
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last_ping = time.monotonic()

        def ping_or_closed(timeout: float) -> bool:
            """<summary>
            Wait up to timeout, sending a keep alive ping when one is due.
            </summary>
            <param name="timeout">Seconds to wait. 0 polls and returns at
            once.</param>
            <returns>True if the client closed the connection, in which case
            the caller must stop streaming.</returns>
            <remarks>
            The client never sends anything on an event stream, so the socket
            turning readable can only mean it closed. That is how a shut tab
            is noticed at once rather than at the next write, on Windows as
            well as Linux.

            The ping is a comment line, which an EventSource ignores. It
            exists to keep anything between the two ends from dropping an
            idle connection, and to make a dead one fail here rather than
            silently persist.
            </remarks>
            """
            nonlocal last_ping
            # The client never sends on an event stream, so the socket turning
            # readable means it closed. This is how a closed tab is noticed at
            # once, on Windows too.
            readable, _, _ = select.select([self.connection], [], [], timeout)
            if readable:
                return True
            if time.monotonic() - last_ping >= PING_SECONDS:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                last_ping = time.monotonic()
            return False

        try:
            while not self.server.stopping.is_set():
                controller = self.server.holder.controller
                if controller is None:
                    # Between decks: tell the page, then hold the stream open and
                    # wait, rather than closing and forcing a reconnect.
                    self._write_event({"type": "waiting", "reason": "deck not connected"})
                    while (not self.server.stopping.is_set()
                           and self.server.holder.controller is None):
                        if ping_or_closed(0.25):
                            return
                    continue
                subscription = controller.hub.subscribe()
                try:
                    self._write_event({"type": "hello", "state": controller.snapshot()})
                    while not self.server.stopping.is_set():
                        if self.server.holder.controller is not controller:
                            break  # deck changed: the outer loop picks up the new one
                        try:
                            event = subscription.get(timeout=0.25)
                        except queue.Empty:
                            if ping_or_closed(0):
                                return
                            continue
                        self._write_event(event)
                finally:
                    controller.hub.unsubscribe(subscription)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _write_event(self, event: dict) -> None:
        """
        <summary>
        Write one server sent event frame and flush it.
        </summary>
        <param name="event">A JSON serialisable dict.</param>
        """
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _page(self) -> None:
        """<summary>
        POST /api/page: switch to a named page, or to the next or previous
        one.
        </summary>
        <remarks>
        The name is only checked for being a non empty string here. Which
        pages exist is the controller's business, and it is the controller
        thread that looks it up, so an unknown name is ignored there rather
        than answered with an error here.

        Queued and answered at once, so the reply says the request was
        accepted, not that the deck has redrawn. The event stream reports the
        change when it actually happens.
        </remarks>
        """
        controller = self._controller()
        if controller is None:
            return
        data = self._read_json()
        if data is None:
            return
        target = data.get("page")
        if not isinstance(target, str) or not target:
            return self._send_json({"error": "'page' must be a page name, 'next' or 'previous'"}, HTTPStatus.BAD_REQUEST)
        controller.submit(lambda: controller.switch_page(target))
        self._send_json({"ok": True})

    def _brightness(self) -> None:
        """<summary>
        POST /api/brightness: set the backlight, or step it by an amount.
        </summary>
        <remarks>
        One of ``value`` and ``delta`` is enough and both are passed down;
        the controller decides what to do with them and clamps the result to
        the range the deck accepts.

        Booleans are refused explicitly, because in Python True is an integer
        and would otherwise be taken as a brightness of 1. JSON's true
        arriving in this field is a bug in the caller, not a request for a
        nearly dark deck.
        </remarks>
        """
        controller = self._controller()
        if controller is None:
            return
        data = self._read_json()
        if data is None:
            return
        value, delta = data.get("value"), data.get("delta")
        if not any(isinstance(x, int) and not isinstance(x, bool) for x in (value, delta)):
            return self._send_json({"error": "'value' or 'delta' must be a whole number"}, HTTPStatus.BAD_REQUEST)
        controller.submit(lambda: controller.set_brightness(value, delta))
        self._send_json({"ok": True})

    def _press(self) -> None:
        """<summary>
        POST /api/press: act as if a key on the deck had been pressed.
        </summary>
        <remarks>
        This runs the key's real action, exactly as a finger on the deck
        would, which is how a configured key is tried from the page without
        reaching for the hardware. It is not a pretend press: whatever the
        key is bound to actually happens.

        The gesture picks which of the three bindings runs, so a key with a
        long press or a double press action can be tested without having to
        time anything.

        The row and column go through the layout module's key numbering and
        straight back again. That looks redundant and is not: it is what
        validates the position against the real grid and what rejects the
        display strip, which has no keys under it to press.
        </remarks>
        """
        controller = self._controller()
        if controller is None:
            return
        data = self._read_json()
        if data is None:
            return
        try:
            key = layout.key_number(int(data["row"]), int(data["column"]))
        except (KeyError, TypeError, ValueError):
            return self._send_json({"error": "'row' and 'column' must be within the key grid"}, HTTPStatus.BAD_REQUEST)
        if layout.is_strip(key):
            return self._send_json({"error": "the display strip cannot be pressed"}, HTTPStatus.BAD_REQUEST)
        gesture = data.get("gesture", "press")
        if gesture not in ("press", "long", "double"):
            return self._send_json({"error": "'gesture' must be press, long or double"}, HTTPStatus.BAD_REQUEST)
        row, column = layout.position(key)
        controller.submit(lambda: controller.test_press(row, column, gesture))
        self._send_json({"ok": True})

    def _wake(self) -> None:
        """<summary>
        POST /api/wake: bring the deck back from sleep.
        </summary>
        <remarks>
        Does nothing when the deck is already awake, and that check is made
        on the controller thread rather than here, because whether it is
        asleep can change between the two. Waking an awake deck would redraw
        every tile for nothing.

        Always answers ok. From the page's point of view "make it awake"
        succeeded either way.
        </remarks>
        """
        controller = self._controller()
        if controller is None:
            return
        controller.submit(lambda: controller.wake() if controller.asleep else None)
        self._send_json({"ok": True})

    def _sleep(self) -> None:
        """<summary>
        POST /api/sleep: put the deck to sleep now.
        </summary>
        <remarks>
        Sleep here means dark and idle, nothing more: the deck stays open,
        stays listening for key presses and keeps its images, and a press or
        a wake request brings it straight back. Nothing is closed, reset or
        cleared.

        Queued like every other action, so this returns before the deck has
        actually gone dark.
        </remarks>
        """
        controller = self._controller()
        if controller is None:
            return
        controller.submit(controller.sleep_deck)
        self._send_json({"ok": True})
