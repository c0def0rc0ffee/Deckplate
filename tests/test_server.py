"""<summary>
The HTTP channel, exercised over real sockets on a random port.
</summary>
<remarks>
Real sockets are used on purpose. Most of what is being checked lives in the
request handling itself, headers, bodies, keep alive and streaming, and calling
the handlers directly would test none of it. Port 0 lets the suite run twice at
once and on a machine where the daemon is already running.

A large part of this file is security rather than function, and it is worth
reading as such. This server sits on the loopback interface of a desktop and
will press keys, run commands and rewrite the configuration file when asked, so
the only thing between a web page in the user's browser and their keyboard is
the checks pinned here: the origin and host checks that keep other websites
out, the content type requirement that denies a cross origin form its one free
shot, and the token when one is configured. Each of those tests names a real
attack. None of them should be relaxed to make a client simpler.
</remarks>
"""

import http.client
import io
import json
import threading
import time

import pytest
from PIL import Image

from deckplate import config as cfg
from deckplate import images, server
from deckplate.config import ServerSettings
from deckplate.controller import EventHub
from deckplate.layout import STRIP_COLUMN

TEXT = """
[[pages]]
name = "Main"
[[pages.keys]]
row = 0
column = 0
label = "Web"
action = { type = "url", url = "https://example.org" }
"""


class FakeSpec:
    """
    <summary>
    A deck spec with the Soomfon id and nothing else.
    </summary>
    """
    name = "Fake deck"
    usb_id = "1500:3003"


class FakeDeck:
    """
    <summary>
    A deck with a fake spec and a 95 px tile.
    </summary>
    """
    spec = FakeSpec()
    image_size = 95


class FakeController:
    """<summary>
    Just enough of Controller for the server: state, tiles, hub, a command queue.
    </summary>
    <remarks>
    A fake rather than the real controller because the real one owns a deck and a
    thread. It records what it was asked to do instead of doing it, so the tests
    can assert on the command the endpoint queued rather than on its effect.

    The fake has to keep the real contracts or it proves nothing. The two preview
    methods draw for the page and never send to the deck, and submit runs the
    command immediately where the real one queues it for the controller thread.
    </remarks>
    """

    def __init__(self, config):
        """
        <summary>
        A controller over a fake deck with two cached tiles, recording everything asked of it.
        </summary>
        <param name="config">The loaded config.</param>
        """
        self.config = config
        self.deck = FakeDeck()
        self.hub = EventHub()
        self.ran = []
        self.asleep = False
        self.brightness = 80
        self._tiles = {(0, 0): images.solid((200, 0, 0), 95), (1, STRIP_COLUMN): images.solid((0, 0, 200), 95)}

    def snapshot(self):
        """
        <summary>
        The state the page reads: page, pages, brightness and asleep.
        </summary>
        <returns>A dict.</returns>
        """
        return {"page": "Main", "pages": ["Main"], "brightness": self.brightness, "asleep": self.asleep}

    def tile_image(self, row, column):
        """
        <summary>
        The cached tile at a position, or None.
        </summary>
        <param name="row">Row.</param>
        <param name="column">Column.</param>
        """
        return self._tiles.get((row, column))

    def preview_tile(self, row, column, scale=1):
        # Mirrors the real contract: scale 1 is the cached hardware tile, above
        # that it is drawn larger, and nothing is ever sent to the deck.
        """
        <summary>
        The cached tile at scale 1, a larger solid tile above that; nothing is ever sent to the deck.
        </summary>
        <param name="row">Row.</param>
        <param name="column">Column.</param>
        <param name="scale">1 for the cached tile.</param>
        <returns>An image or None.</returns>
        """
        tile = self._tiles.get((row, column))
        if tile is None or scale == 1:
            return tile
        return images.solid((200, 0, 0), 95 * scale)

    def preview_animation(self, row, column, scale=1):
        # Only (0, 0) animates in this fake. Same contract: drawn for the page,
        # never sent to the deck.
        """
        <summary>
        A two frame GIF for position (0, 0) only, never sent to the deck.
        </summary>
        <param name="row">Row.</param>
        <param name="column">Column.</param>
        <param name="scale">Tile scale.</param>
        <returns>GIF bytes or None.</returns>
        """
        if (row, column) != (0, 0):
            return None
        from deckplate import animations
        frames = [images.solid((200, 0, 0), 95 * scale), images.solid((0, 200, 0), 95 * scale)]
        return animations.encode_gif(frames, 2)

    def submit(self, command):
        """
        <summary>
        Record the command and run it at once.
        </summary>
        <param name="command">A callable.</param>
        """
        self.ran.append(command)
        command()

    def switch_page(self, target):
        """
        <summary>
        Record the call.
        </summary>
        <param name="target">Page name.</param>
        """
        self.ran.append(("page", target))

    def set_brightness(self, value=None, delta=None):
        """
        <summary>
        Record the call.
        </summary>
        <param name="value">Absolute, or None.</param>
        <param name="delta">Relative, or None.</param>
        """
        self.ran.append(("brightness", value, delta))

    def handle_event(self, event):
        """
        <summary>
        Record the key and whether it was pressed.
        </summary>
        <param name="event">A key event.</param>
        """
        self.ran.append(("event", event.key, event.pressed))

    def test_press(self, row, column, gesture="press"):
        """
        <summary>
        Record the call.
        </summary>
        <param name="row">Row.</param>
        <param name="column">Column.</param>
        <param name="gesture">press, long or double.</param>
        """
        self.ran.append(("press", row, column, gesture))

    def wake(self):
        """
        <summary>
        Record the call.
        </summary>
        """
        self.ran.append(("wake",))

    def reload_from_disk(self):
        """
        <summary>
        Reload the config from its file and record it.
        </summary>
        """
        self.config = cfg.load(self.config.path)
        self.ran.append(("reload",))


@pytest.fixture
def api(tmp_path):
    """<summary>
    A running server on a random port with a fake controller behind it.
    </summary>
    <remarks>
    Port 0 asks the operating system for a free port, so the suite never collides
    with a real daemon or with a second copy of itself. The log is swallowed to
    keep the test output readable, and the server is stopped on the way out whether
    the test passed or not, because a leaked server thread holds its port.
    </remarks>
    """
    path = tmp_path / "config.toml"
    path.write_text(TEXT, encoding="utf-8")
    holder = server.ControllerHolder()
    holder.controller = FakeController(cfg.load(path))
    srv = server.ApiServer(ServerSettings(port=0), holder, log=lambda t: None).start()
    yield srv, holder
    srv.stop()


def request(srv, method, path, body=None, headers=None, conn=None):
    """<summary>
    Make one request and return the status, content type and body.
    </summary>
    <param name="conn">An existing connection to reuse. When given it is left open
    so the same socket can carry the next request.</param>
    <returns>A (status, content type, body bytes) tuple.</returns>
    <remarks>
    The content type is filled in only when there is a body, and defaults to TOML
    for the configuration endpoint and JSON everywhere else. That default is a
    convenience, not the behaviour under test: the tests that care about content
    type pass it explicitly, because the server treats a missing or wrong one as a
    refusal rather than a detail.
    </remarks>
    """
    own = conn is None
    if own:
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/toml" if path == "/api/config" else "application/json")
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    if own:
        conn.close()
    return resp.status, resp.getheader("Content-Type", ""), raw


def test_placeholder_page(api):
    """<summary>
    The root path serves the configuration page.
    </summary>
    """
    srv, _ = api
    status, ctype, raw = request(srv, "GET", "/")
    assert status == 200 and ctype.startswith("text/html") and b"Deckplate" in raw


def test_state(api):
    """<summary>
    The state endpoint answers with the controller's snapshot as JSON.
    </summary>
    <remarks>
    This is what the page polls to stay in step with the deck, so both the content
    type and the shape matter: a page that cannot read the current page name has no
    way to show which one is active.
    </remarks>
    """
    srv, _ = api
    status, ctype, raw = request(srv, "GET", "/api/state")
    assert status == 200 and ctype.startswith("application/json")
    assert json.loads(raw)["page"] == "Main"


def test_tiles(api):
    """<summary>
    A tile is fetched as a JPEG by row and column, with a 404 for an empty position
    and a 400 for a position that is not on the deck.
    </summary>
    <remarks>
    The distinction between the two failures is deliberate. A valid but empty
    position is a normal thing for the page to ask about and answers 404, while a
    row or column off the deck, or a path that is not a pair of numbers at all, is
    a bug in the page and answers 400. Collapsing them would hide the bug.
    </remarks>
    """
    srv, _ = api
    status, ctype, raw = request(srv, "GET", "/api/tiles/0/0.jpg")
    assert status == 200 and ctype == "image/jpeg" and raw[:3] == b"\xff\xd8\xff"
    assert request(srv, "GET", f"/api/tiles/1/{STRIP_COLUMN}.jpg")[0] == 200
    assert request(srv, "GET", "/api/tiles/2/2.jpg")[0] == 404
    assert request(srv, "GET", "/api/tiles/9/9.jpg")[0] == 400
    assert request(srv, "GET", "/api/tiles/x.jpg")[0] == 400


def test_tiles_scale(api):
    """<summary>
    The page may ask for a panel drawn larger than the deck's own image size.
    </summary>
    <remarks>
    The configuration page shows tiles much bigger than 95 pixels, and upscaling
    the deck's own JPEG in the browser looks poor, so the tile is drawn again at
    the larger size. It is still only drawn, never sent to the hardware: the deck
    has one image size and this must not become a way to reach it.

    Out of range and nonsense are refused rather than clamped, so a page bug is
    visible instead of quietly drawing the wrong size.
    </remarks>
    """
    srv, _ = api
    native = request(srv, "GET", "/api/tiles/0/0.jpg")[2]
    bigger = request(srv, "GET", "/api/tiles/0/0.jpg?scale=2")
    assert bigger[0] == 200 and bigger[1] == "image/jpeg"
    assert Image.open(io.BytesIO(native)).width == 95
    assert Image.open(io.BytesIO(bigger[2])).width == 190

    # Out of range and nonsense are refused rather than clamped, so a page bug
    # is visible instead of quietly drawing the wrong size.
    for bad in ("0", "4", "99", "x", "2.5", ""):
        status, _, body = request(srv, "GET", f"/api/tiles/0/0.jpg?scale={bad}")
        assert status == 400, f"scale={bad!r} should be refused"
        assert b"scale" in body


def test_animated_tiles_come_as_a_gif_loop(api):
    """<summary>
    An animated panel is fetched as a GIF; a still one says so with a 404.
    </summary>
    <remarks>
    The extension chooses the format, so the page asks for a GIF and finds out from
    the status code whether the key animates at all. That keeps the browser from
    having to guess, and keeps a still key from being served a one frame GIF that
    would loop forever for nothing.
    </remarks>
    """
    srv, _ = api
    status, ctype, raw = request(srv, "GET", "/api/tiles/0/0.gif?scale=2")
    assert status == 200 and ctype == "image/gif" and raw[:6] in (b"GIF89a", b"GIF87a")
    with Image.open(io.BytesIO(raw)) as gif:
        assert gif.is_animated and gif.width == 190
    assert request(srv, "GET", "/api/tiles/1/1.gif")[0] == 404
    assert request(srv, "GET", "/api/tiles/9/9.gif")[0] == 400
    assert request(srv, "GET", "/api/tiles/0/0.gif?scale=7")[0] == 400


def test_config_get_and_put(api):
    """<summary>
    The configuration file can be read and written whole, and a file that fails
    validation is rejected without being saved.
    </summary>
    <remarks>
    The rejection check is the important half, and it checks the file on disk, not
    just the status code. An invalid file written and then rejected would leave the
    daemon unable to start on its next run, so validation has to happen before the
    write rather than after it. A good write reloads the controller so the deck
    follows the file immediately.
    </remarks>
    """
    srv, holder = api
    status, ctype, raw = request(srv, "GET", "/api/config")
    assert status == 200 and "toml" in ctype and b'name = "Main"' in raw

    bad = request(srv, "PUT", "/api/config", body=b"[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 9\ncolumn = 0")
    assert bad[0] == 400 and b"between 0 and 2" in bad[2]
    assert b"row = 9" not in holder.controller.config.path.read_bytes()

    good = TEXT.replace('name = "Main"', 'name = "Renamed"').encode()
    status, _, raw = request(srv, "PUT", "/api/config", body=good)
    assert status == 200 and json.loads(raw) == {"ok": True}
    assert ("reload",) in holder.controller.ran
    assert holder.controller.config.pages[0].name == "Renamed"


def test_commands_are_queued_for_the_controller(api):
    """<summary>
    Page, brightness, press and wake all reach the controller as queued commands.
    </summary>
    <remarks>
    The endpoints queue work rather than doing it, because the deck is driven from
    one thread and a web request must not write to USB from another. The gesture
    parameter is checked against the known set, so an unknown gesture is refused
    rather than silently treated as a plain press.

    A bodyless POST with no content type is what a cross origin form can send, and
    it is refused with a 415. That is not pedantry about content types: it is the
    check that stops a page on another site from pressing keys on this deck. Wake
    on an already awake deck does nothing, which is why the recorded list stays
    empty for it.
    </remarks>
    """
    srv, holder = api
    ran = holder.controller.ran
    assert request(srv, "POST", "/api/page", body={"page": "next"})[0] == 200
    assert request(srv, "POST", "/api/brightness", body={"value": 40})[0] == 200
    assert request(srv, "POST", "/api/brightness", body={"delta": -5})[0] == 200
    assert request(srv, "POST", "/api/press", body={"row": 0, "column": 0})[0] == 200
    assert ("press", 0, 0, "press") in ran
    assert request(srv, "POST", "/api/press", body={"row": 1, "column": 2, "gesture": "long"})[0] == 200
    assert ("press", 1, 2, "long") in ran
    assert request(srv, "POST", "/api/press", body={"row": 0, "column": 0, "gesture": "sideways"})[0] == 400
    assert request(srv, "POST", "/api/wake", body={})[0] == 200
    # a bodyless POST with no content type is what a cross origin form can send
    assert request(srv, "POST", "/api/wake")[0] == 415
    assert ("page", "next") in ran
    assert ("brightness", 40, None) in ran and ("brightness", None, -5) in ran
    assert ("press", 0, 0, "press") in ran  # row 0 column 0, the plain action
    assert not any(r == ("wake",) for r in ran)  # not asleep, so wake does nothing


def test_bad_commands(api):
    """<summary>
    Wrong types, missing fields, unparseable bodies and unknown paths are all
    refused with a 400 or a 404.
    </summary>
    <remarks>
    Two cases are more than type checking. A brightness of True is rejected even
    though a boolean is an integer in Python, because accepting it would set the
    deck to brightness 1. A press aimed at the strip column is rejected because the
    strip has no action to run.

    The traversal attempt on the static path is here rather than in its own test
    because it belongs to the same idea: a request that does not make sense is
    answered with a status code and nothing else.
    </remarks>
    """
    srv, _ = api
    assert request(srv, "POST", "/api/page", body={"page": 3})[0] == 400
    assert request(srv, "POST", "/api/brightness", body={"value": "bright"})[0] == 400
    assert request(srv, "POST", "/api/brightness", body={"value": True})[0] == 400
    assert request(srv, "POST", "/api/press", body={"row": 0, "column": STRIP_COLUMN})[0] == 400
    assert request(srv, "POST", "/api/press", body={"row": 0})[0] == 400
    assert request(srv, "POST", "/api/page", body=b"not json")[0] == 400
    assert request(srv, "POST", "/api/nothing", body={})[0] == 404
    assert request(srv, "GET", "/api/nothing")[0] == 404
    assert request(srv, "GET", "/static/../config.py")[0] in (400, 404)


def test_disconnected_deck_gives_503(api):
    """<summary>
    With no deck attached, every endpoint that needs one answers 503.
    </summary>
    <remarks>
    503 rather than 500 tells the page this is temporary and worth retrying, which
    is exactly right for an unplugged deck: it is expected to come back, and the
    page should recover on its own when it does.
    </remarks>
    """
    srv, holder = api
    holder.controller = None
    assert request(srv, "GET", "/api/state")[0] == 503
    assert request(srv, "POST", "/api/page", body={"page": "next"})[0] == 503


def test_bodies_are_consumed_on_error_so_keep_alive_stays_in_sync(api):
    """<summary>
    A request refused before its body is read still leaves the connection usable
    for the next request.
    </summary>
    <remarks>
    This is the subtlest fault in the whole server. An unread request body stays in
    the socket buffer, and the next request on that keep alive connection starts
    reading in the middle of it, so it is misparsed or hangs. The symptom is a page
    that works until the first rejected request and then fails at random
    afterwards, which points nowhere near the real cause.

    Both rejection paths are exercised on the same connection, the missing deck and
    the refused origin, because each is a separate early return that has to drain
    the body before it answers.
    </remarks>
    """
    srv, holder = api
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        holder.controller_backup = holder.controller
        holder.controller = None
        assert request(srv, "POST", "/api/page", body={"page": "next"}, conn=conn)[0] == 503
        holder.controller = holder.controller_backup
        # same connection: the next request must not be polluted by the last body
        assert request(srv, "GET", "/api/state", conn=conn)[0] == 200
        assert request(srv, "POST", "/api/page", body={"page": "x"}, headers={"Origin": "http://evil.example"}, conn=conn)[0] == 403
        assert request(srv, "GET", "/api/state", conn=conn)[0] == 200
    finally:
        conn.close()


def test_other_websites_are_kept_out(api):
    """<summary>
    Requests carrying another site's origin, an unrecognised host, or a content
    type a form could send are all refused.
    </summary>
    <remarks>
    This is the main defence, and each line is a real attack. A page on another
    site can make the browser send requests here with the user's own network
    access, so the origin is checked and anything that is not this server is
    refused, including the "null" origin a sandboxed frame sends.

    The host check is DNS rebinding: an attacker points their own name at loopback,
    so the origin looks legitimate while the request is aimed at this machine.
    Refusing a host that is not a loopback name closes it.

    The content type check is the last piece. A form can be submitted cross origin
    without the browser asking permission first, but only with a few simple content
    types, so requiring JSON means any real attempt has to go through a preflight,
    which the origin check then refuses outright and without permissive headers.

    Nothing here should be loosened to make a client easier to write. Anything that
    weakens these gives a web page the ability to press keys on the user's machine.
    </remarks>
    """
    srv, holder = api
    ok_origin = f"http://127.0.0.1:{srv.port}"
    assert request(srv, "POST", "/api/page", body={"page": "next"}, headers={"Origin": ok_origin})[0] == 200
    assert request(srv, "POST", "/api/page", body={"page": "next"}, headers={"Origin": "http://evil.example"})[0] == 403
    assert request(srv, "GET", "/api/state", headers={"Origin": "null"})[0] == 403
    # DNS rebinding: a Host header that is not a loopback name
    assert request(srv, "GET", "/api/state", headers={"Host": "deck.evil.example"})[0] == 403
    assert request(srv, "GET", "/api/state", headers={"Host": f"localhost:{srv.port}"})[0] == 200
    # a browser can send text/plain cross origin without a preflight; JSON it cannot
    assert request(srv, "POST", "/api/press", body=b'{"row":0,"column":0}', headers={"Content-Type": "text/plain"})[0] == 415
    assert request(srv, "PUT", "/api/config", body=b"x", headers={"Content-Type": "text/plain"})[0] == 415
    assert not any(r == ("event", 13, True) for r in holder.controller.ran)
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.request("OPTIONS", "/api/press", headers={"Origin": "http://evil.example",
                                                   "Access-Control-Request-Method": "POST"})
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 403 and resp.getheader("Access-Control-Allow-Origin") is None
    conn.close()


def test_bad_content_length_and_oversized_body(api):
    """<summary>
    An unparseable content length is a 400, and one larger than the limit is a 413
    answered before the body is read.
    </summary>
    <remarks>
    The oversized case never sends the body at all, which is the point: the server
    must decide from the header rather than reading gigabytes into memory first. A
    missing limit here would be a way to exhaust memory on the machine from a
    single request.
    </remarks>
    """
    srv, _ = api
    assert request(srv, "POST", "/api/wake", headers={"Content-Length": "abc"})[0] == 400
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.putrequest("PUT", "/api/config")
    conn.putheader("Content-Type", "application/toml")
    conn.putheader("Content-Length", str(server.MAX_BODY_BYTES + 1))
    conn.endheaders()
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 413
    conn.close()


def test_config_put_keeps_bytes_exactly_and_writes_atomically(api):
    """<summary>
    The configuration file is stored byte for byte as sent, through a temporary
    file that is not left behind.
    </summary>
    <remarks>
    Byte exactness is what keeps the file hand editable. Rewriting it through a
    parser would lose comments, ordering and the line endings someone chose, so
    saving from the page would quietly reformat a file its owner maintains by hand.
    The round trip assertion is the one that catches that: what was read back has
    to save unchanged.

    The atomic write matters because this file is the daemon's whole configuration.
    A partial write during a crash or a power cut leaves a file that will not parse,
    so the content goes to a temporary file that replaces the original in one step.
    </remarks>
    """
    srv, holder = api
    path = holder.controller.config.path
    crlf = TEXT.replace("\n", "\r\n").encode()
    assert request(srv, "PUT", "/api/config", body=crlf)[0] == 200
    assert path.read_bytes() == crlf
    assert not path.with_name(path.name + ".tmp").exists()
    # saving what was read back must round trip unchanged
    _, _, raw = request(srv, "GET", "/api/config")
    assert request(srv, "PUT", "/api/config", body=raw)[0] == 200
    assert path.read_bytes() == crlf


def test_stop_returns_promptly_with_an_events_client_connected(tmp_path):
    """<summary>
    Stopping the server returns quickly even with an event stream still open.
    </summary>
    <remarks>
    An event stream is a request that never finishes by design, so a naive shutdown
    waits for it forever. That would hang the daemon on exit with the configuration
    page merely open in a background tab, and the only way out would be to kill it.
    The three second bound is loose on purpose: the fault being caught is an
    indefinite hang, not a slow tenth of a second.
    </remarks>
    """
    path = tmp_path / "config.toml"
    path.write_text(TEXT, encoding="utf-8")
    holder = server.ControllerHolder()
    holder.controller = FakeController(cfg.load(path))
    srv = server.ApiServer(ServerSettings(port=0), holder, log=lambda t: None).start()
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    assert resp.fp.readline().startswith(b"data: ")
    started = time.monotonic()
    srv.stop()
    assert time.monotonic() - started < 3
    resp.close()
    conn.close()


def test_document_get_and_put(api):
    """<summary>
    The configuration is also offered as structured JSON, and writing it back
    validates before saving.
    </summary>
    <remarks>
    This is the endpoint the page actually edits through, with the TOML endpoint
    kept for people who prefer the file. Both write the same file, so both have to
    validate to the same standard: the rejected row number here produces the same
    message the TOML path gives.

    The shape is checked too, because a request body that is a list rather than an
    object would otherwise reach the writer and produce a file nothing can read.
    </remarks>
    """
    srv, holder = api
    status, ctype, raw = request(srv, "GET", "/api/document")
    assert status == 200 and ctype.startswith("application/json")
    doc = json.loads(raw)
    assert doc["pages"][0]["keys"][0]["label"] == "Web"

    doc["pages"][0]["keys"][0]["label"] = "News"
    doc["pages"].append({"name": "Extra", "keys": []})
    assert request(srv, "PUT", "/api/document", body=doc)[0] == 200
    assert ("reload",) in holder.controller.ran
    text = holder.controller.config.path.read_text(encoding="utf-8")
    assert 'label = "News"' in text and 'name = "Extra"' in text

    doc["pages"][0]["keys"][0]["row"] = 7
    status, _, raw = request(srv, "PUT", "/api/document", body=doc)
    assert status == 400 and b"between 0 and 2" in raw
    assert request(srv, "PUT", "/api/document", body=[1, 2])[0] == 400
    assert request(srv, "PUT", "/api/document", body=b"nope")[0] == 400


def test_images_list_get_upload(api):
    """<summary>
    Pictures can be listed, fetched and uploaded into the configuration folder,
    with the filename supplied in a header.
    </summary>
    <remarks>
    Everything about the name is treated as hostile, because this endpoint writes
    files to disk. A name containing a traversal is refused, a name starting with a
    dot is refused, and a missing name is refused rather than being invented.
    Fetching applies the same rule, so a path that climbs out of the folder is a
    404 and not the contents of the configuration file.

    The content type has two jobs here: it decides the extension when the name has
    none, and it refuses anything that is not a picture. The empty body case is
    separate because a zero byte file would list and preview as a broken picture
    forever.
    </remarks>
    """
    srv, holder = api
    assert json.loads(request(srv, "GET", "/api/images")[2])["images"] == []
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    status, _, raw = request(srv, "POST", "/api/images", body=png,
                             headers={"Content-Type": "image/png", "X-Filename": "My Icon.png"})
    assert status == 200 and json.loads(raw)["path"] == "images/My Icon.png"
    folder = holder.controller.config.base_dir / "images"
    assert (folder / "My Icon.png").read_bytes() == png
    assert json.loads(request(srv, "GET", "/api/images")[2])["images"] == ["My Icon.png"]
    status, ctype, raw = request(srv, "GET", "/api/images/My%20Icon.png")
    assert status == 200 and ctype == "image/png" and raw == png
    # a name without an extension gets one from the content type
    status, _, raw = request(srv, "POST", "/api/images", body=png, headers={"Content-Type": "image/jpeg", "X-Filename": "photo"})
    assert json.loads(raw)["name"] == "photo.jpg"
    # bad names and shapes
    assert request(srv, "POST", "/api/images", body=png, headers={"Content-Type": "image/png", "X-Filename": "../evil.png"})[0] == 400
    assert request(srv, "POST", "/api/images", body=png, headers={"Content-Type": "image/png", "X-Filename": ".hidden.png"})[0] == 400
    assert request(srv, "POST", "/api/images", body=png, headers={"Content-Type": "image/png"})[0] == 400
    assert request(srv, "POST", "/api/images", body=b"", headers={"Content-Type": "image/png", "X-Filename": "x.png"})[0] == 400
    assert request(srv, "POST", "/api/images", body=png, headers={"Content-Type": "text/plain", "X-Filename": "x.png"})[0] == 415
    assert request(srv, "GET", "/api/images/../config.toml")[0] == 404
    assert request(srv, "GET", "/api/images/nothing.png")[0] == 404


def test_images_report_usage_and_delete_only_when_unused(api):
    """<summary>
    The listing says where each picture is used, and deleting one that is still in
    use is refused with a 409.
    </summary>
    <remarks>
    Deleting a picture a key depends on cannot be undone from the page, and the
    result is a key that shows the missing picture marker with no clue what it used
    to be. Refusing the delete and naming the page and key that use it is the whole
    feature; the listing exists to make that answer possible before the attempt.

    The usage strings are counted from one for people reading them, while the
    configuration file counts rows and columns from zero. That is deliberate and is
    why the expected text reads row 1, column 1 for the key at position 0, 0.

    The last line keeps delete under the same content type rule as every other
    change, so it cannot be reached by a cross origin form either.
    </remarks>
    """
    srv, holder = api
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    for name in ("used.png", "spare.png"):
        assert request(srv, "POST", "/api/images", body=png, headers={"Content-Type": "image/png", "X-Filename": name})[0] == 200
    # point the Web key at used.png through the document
    doc = json.loads(request(srv, "GET", "/api/document")[2])
    doc["pages"][0]["keys"][0]["image"] = "images/used.png"
    assert request(srv, "PUT", "/api/document", body=doc)[0] == 200
    listing = json.loads(request(srv, "GET", "/api/images")[2])
    assert listing["images"] == ["spare.png", "used.png"]
    by_name = {d["name"]: d for d in listing["details"]}
    assert by_name["used.png"]["used_by"] == ["Main: row 1, column 1"]
    assert by_name["spare.png"]["used_by"] == [] and by_name["spare.png"]["bytes"] == len(png)
    assert listing["folder"].endswith("images")

    status, _, raw = request(srv, "DELETE", "/api/images/used.png", body={})
    assert status == 409 and b"still used" in raw
    assert request(srv, "DELETE", "/api/images/spare.png", body={})[0] == 200
    assert json.loads(request(srv, "GET", "/api/images")[2])["images"] == ["used.png"]
    assert request(srv, "DELETE", "/api/images/spare.png", body={})[0] == 404
    assert request(srv, "DELETE", "/api/images/../config.toml", body={})[0] == 404
    assert request(srv, "DELETE", "/api/images/used.png")[0] == 415  # needs the JSON content type like every mutation


def test_a_wallpaper_counts_as_a_use_of_a_picture(api):
    """<summary>
    A page's wallpaper keeps its picture from being deleted, like a key's picture
    does.
    </summary>
    <remarks>
    The wallpaper lives on the page rather than on a key, so a usage scan that only
    walked the keys would miss it and report the picture as spare. Deleting it
    would then blank the backdrop of a whole page. The closing check is that a
    refused delete really changed nothing: the reference is still there afterwards.
    </remarks>
    """
    srv, _ = api
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    assert request(srv, "POST", "/api/images", body=png, headers={"Content-Type": "image/png", "X-Filename": "wall.png"})[0] == 200
    doc = json.loads(request(srv, "GET", "/api/document")[2])
    assert doc["pages"][0]["wallpaper"] is None
    doc["pages"][0]["wallpaper"] = "images/wall.png"
    assert request(srv, "PUT", "/api/document", body=doc)[0] == 200
    by_name = {d["name"]: d for d in json.loads(request(srv, "GET", "/api/images")[2])["details"]}
    assert by_name["wall.png"]["used_by"] == ["Main: wallpaper"]
    assert request(srv, "DELETE", "/api/images/wall.png", body={})[0] == 409
    assert json.loads(request(srv, "GET", "/api/document")[2])["pages"][0]["wallpaper"] == "images/wall.png"


def test_geocode_proxy(api, monkeypatch):
    """<summary>
    Place lookup is proxied through the daemon, with a missing name refused and an
    upstream failure reported as 502.
    </summary>
    <remarks>
    It is proxied rather than called from the browser so the page needs no network
    access of its own and no key or account is exposed to it. The upstream call is
    replaced here, so the suite never depends on the internet being up.

    502 rather than 500 says the fault is upstream and not in the daemon, which is
    what the page needs in order to say something useful instead of implying the
    deck is broken.
    </remarks>
    """
    from deckplate import weather

    srv, _ = api
    monkeypatch.setattr(weather, "geocode", lambda name: [weather.Place("Guernsey", "", "Guernsey", 49.45, -2.54)])
    status, _, raw = request(srv, "GET", "/api/geocode?name=Guernsey")
    assert status == 200 and json.loads(raw)["places"][0]["latitude"] == 49.45
    assert request(srv, "GET", "/api/geocode")[0] == 400
    monkeypatch.setattr(weather, "geocode", lambda name: (_ for _ in ()).throw(ConnectionError("offline")))
    assert request(srv, "GET", "/api/geocode?name=x")[0] == 502


def test_sleep_command(api):
    """<summary>
    The sleep endpoint reaches the controller's sleep call.
    </summary>
    """
    srv, holder = api
    holder.controller.sleep_deck = lambda: holder.controller.ran.append(("sleep",))
    assert request(srv, "POST", "/api/sleep", body={})[0] == 200
    assert ("sleep",) in holder.controller.ran


def test_static_page_and_assets_are_served(api):
    """<summary>
    The page and its script and stylesheet are served with the right content types.
    </summary>
    <remarks>
    Browsers refuse to run a script or apply a stylesheet served under the wrong
    type, so this is not cosmetic: getting either wrong gives a page that loads and
    then does nothing at all, with the reason only visible in the browser console.
    </remarks>
    """
    srv, _ = api
    status, ctype, raw = request(srv, "GET", "/")
    assert status == 200 and ctype.startswith("text/html") and b"app.js" in raw
    assert request(srv, "GET", "/static/app.js")[1].startswith("text/javascript")
    assert request(srv, "GET", "/static/app.css")[1].startswith("text/css")


def test_dropped_connections_are_not_logged_but_other_errors_are(tmp_path):
    """<summary>
    A client hanging up is silent, while any other handler error is logged.
    </summary>
    <remarks>
    A browser closing a tab resets the connection, and an event stream makes that
    routine. Logging it would fill the log with noise from normal use and bury the
    faults worth reading. The second half is what keeps that from becoming a
    blanket silence: anything that is not a dropped connection still gets logged.
    </remarks>
    """
    path = tmp_path / "config.toml"
    path.write_text(TEXT, encoding="utf-8")
    holder = server.ControllerHolder()
    holder.controller = FakeController(cfg.load(path))
    log = []
    srv = server.ApiServer(ServerSettings(port=0), holder, log=log.append)
    try:
        try:
            raise ConnectionResetError("closed by the remote host")
        except ConnectionResetError:
            srv.handle_error(None, ("127.0.0.1", 1234))
        assert log == []
        try:
            raise RuntimeError("something else")
        except RuntimeError:
            srv.handle_error(None, ("127.0.0.1", 1234))
        assert len(log) == 1 and "something else" in log[0]
    finally:
        srv.server_close()


def test_second_daemon_cannot_take_the_same_port(api):
    """<summary>
    Binding a port that is already in use raises instead of quietly succeeding.
    </summary>
    <remarks>
    Address reuse can let a second bind succeed on some systems, and two daemons
    sharing a port means requests land at random on one or the other. Failing at
    startup is what turns that into a clear message about an instance already
    running.
    </remarks>
    """
    srv, holder = api
    with pytest.raises(OSError):
        server.ApiServer(ServerSettings(port=srv.port), holder, log=lambda t: None)


def test_events_stream_survives_the_deck_going_away_and_coming_back(api):
    """<summary>
    The stream stays open across a disconnect so the page recovers on its own.
    </summary>
    <remarks>
    A 503 or a closed stream while the deck is unplugged would make the browser's
    EventSource give up for good, and the page would never update again after a
    replug. Instead the deck going away sends a "waiting" event and holds the
    connection, and the deck coming back sends a fresh "hello" on the same stream.

    The reconnected deck is a new controller object, not the original one, because
    that is what a replug produces. The stream has to follow the holder rather than
    hold a reference to the controller it started with, or it would go on watching
    an object nothing else is using.
    </remarks>
    """
    srv, holder = api
    original = holder.controller
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    assert resp.status == 200

    def read_event():
        """
        <summary>
        Block until the next server sent event on the response and decode it.
        </summary>
        <returns>The event dict.</returns>
        """
        while True:
            line = resp.fp.readline()
            if line.startswith(b"data: "):
                return json.loads(line[6:])

    assert read_event()["type"] == "hello"
    holder.controller = None                       # deck unplugged
    assert read_event()["type"] == "waiting"       # stream stays open, not 503, not ended
    holder.controller = FakeController(original.config)  # deck back as a new controller
    assert read_event()["type"] == "hello"         # fresh hello, no reconnect needed
    resp.close()
    conn.close()


def test_token_required_when_configured(tmp_path):
    """<summary>
    With a token configured, requests without it are refused and either the header
    or the query parameter satisfies it.
    </summary>
    <remarks>
    The token is what makes binding to anything other than loopback survivable, and
    the configuration refuses a non loopback bind without one. So this test is the
    enforcement half of that rule: if it went red, a deck exposed on a network
    would take commands from anyone who could reach the port.

    The query parameter is accepted because an EventSource cannot set headers, so a
    stream has no other way to authenticate. That is the reason it exists and the
    reason the token should stay short lived and local.
    </remarks>
    """
    path = tmp_path / "config.toml"
    path.write_text(TEXT, encoding="utf-8")
    holder = server.ControllerHolder()
    holder.controller = FakeController(cfg.load(path))
    srv = server.ApiServer(ServerSettings(port=0, token="s3cret"), holder, log=lambda t: None).start()
    try:
        assert request(srv, "GET", "/api/state")[0] == 401
        assert request(srv, "GET", "/api/state", headers={"X-Token": "s3cret"})[0] == 200
        assert request(srv, "GET", "/api/state?token=s3cret")[0] == 200
    finally:
        srv.stop()


def test_events_stream_receives_published_events(api):
    """<summary>
    A subscriber receives the opening state and then every published event, and is
    dropped once its socket closes.
    </summary>
    <remarks>
    The opening hello carries the full state so the page does not need a separate
    fetch to start up, and the wait for the subscriber count before publishing is
    what keeps the test from racing the handler's own subscribe.

    The cleanup half is the part that matters over a long run. A subscriber that
    was not removed when its browser tab closed would have events queued to it
    forever, and a daemon left running for days would accumulate one per page
    refresh until it ran out of memory.
    </remarks>
    """
    srv, holder = api
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type", "").startswith("text/event-stream")

    def read_event():
        """
        <summary>
        Block until the next server sent event on the response and decode it.
        </summary>
        <returns>The event dict.</returns>
        """
        while True:
            line = resp.fp.readline()
            if line.startswith(b"data: "):
                return json.loads(line[6:])

    hello = read_event()
    assert hello["type"] == "hello" and hello["state"]["page"] == "Main"

    # wait until the handler has subscribed, then publish from another thread
    deadline = time.monotonic() + 2
    while holder.controller.hub.subscriber_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    threading.Thread(target=lambda: holder.controller.hub.publish({"type": "key", "key": 13, "pressed": True})).start()
    assert read_event() == {"type": "key", "key": 13, "pressed": True}
    resp.close()  # the response owns the socket; closing the connection alone is not enough
    conn.close()
    # the handler notices the closed socket within one queue poll
    deadline = time.monotonic() + 3
    while holder.controller.hub.subscriber_count and time.monotonic() < deadline:
        time.sleep(0.05)
    assert holder.controller.hub.subscriber_count == 0


def test_server_settings_validation():
    """<summary>
    Server settings are validated when the configuration is parsed, and binding
    beyond loopback without a token is refused outright.
    </summary>
    <remarks>
    This is the rule the token test enforces at request time, stated at load time:
    a bind address reachable from the network requires a token, and a configuration
    without one fails to parse rather than starting an unprotected server. It is
    checked here, beside the server tests, because it is a property of the server
    and not of the configuration format.

    The default with no server section is the safe one, which is what an untouched
    configuration file gets.
    </remarks>
    """
    c = cfg.parse("[server]\nbind = '0.0.0.0'\ntoken = 'abc'\n[[pages]]\nname = 'A'", "/b")
    assert c.server.bind == "0.0.0.0" and c.server.token == "abc"
    with pytest.raises(cfg.ConfigError, match="token"):
        cfg.parse("[server]\nbind = '0.0.0.0'\n[[pages]]\nname = 'A'", "/b")
    with pytest.raises(cfg.ConfigError, match="enabled"):
        cfg.parse("[server]\nenabled = 'yes'\n[[pages]]\nname = 'A'", "/b")
    assert cfg.parse("[[pages]]\nname = 'A'", "/b").server == ServerSettings()


def test_themes_endpoint_lists_and_serves_icons(api):
    """<summary>
    The shipped themes are listed and their icons served as PNGs, with anything
    unsafe answered as a missing file.
    </summary>
    <remarks>
    This is the traversal guard reached the way an attacker would reach it, over
    HTTP rather than through the resolver. The encoded slash is the case worth
    keeping: a name that looks like one path segment in the request line and
    becomes two after decoding is how a nested path slips past a check made too
    early.
    </remarks>
    """
    srv, holder = api
    status, _ctype, raw = request(srv, "GET", "/api/themes")
    assert status == 200
    slugs = {t["slug"] for t in json.loads(raw)["themes"]}
    assert "space-game" in slugs
    status, ctype, raw = request(srv, "GET", "/api/themes/space-game/power.png")
    assert status == 200 and ctype.startswith("image/png") and raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert request(srv, "GET", "/api/themes/space-game/not-an-icon.png")[0] == 404
    assert request(srv, "GET", "/api/themes/space-game/nested%2Fbad.png")[0] == 404
