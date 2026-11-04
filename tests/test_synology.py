"""<summary>
Synology: the reading DSM's replies become, the session that learns where
the APIs live and signs in when the box asks, the monitor driven by hand,
and the runner's press.
</summary>
<remarks>
The fake box answers as a DSM 7 does, by the shapes the Home Assistant
library reads: discovery from ``query.cgi``, everything else from
``entry.cgi``, a session id handed back by login and expected as ``_sid``
afterwards, error 119 for a session that has gone and 400 for a wrong
password. Nothing here goes near a network.
</remarks>
"""

import pytest

from deckplate import synology
from deckplate.actions import ActionRunner
from deckplate.config import Action
from deckplate.synology import SynologyMonitor, SynologyReading, SynologySession, Volume


class Context:
    """<summary>A deck context that does nothing, for a runner under test.</summary>"""

    def switch_page(self, target):
        """<summary>Ignored.</summary>"""

    def set_brightness(self, value=None, delta=None):
        """<summary>Ignored.</summary>"""

    def sleep_deck(self):
        """<summary>Ignored.</summary>"""


class Clock:
    """<summary>A monotonic clock moved by hand.</summary>"""

    def __init__(self):
        """<summary>Start at zero.</summary>"""
        self.t = 0.0

    def __call__(self):
        """<summary>Read the clock.</summary>"""
        return self.t


DISCOVERY = {name: {"maxVersion": v, "minVersion": 1, "path": "entry.cgi"} for name, v in
             ((synology.API_AUTH, 7), (synology.API_STORAGE, 1), (synology.API_UTILIZATION, 1), (synology.API_DSM_INFO, 2))}
STORAGE = {
    "volumes": [{"id": "volume_1", "status": "normal", "size": {"total": "8000000000000", "used": "5000000000000"}},
                {"id": "volume_2", "status": "degraded", "size": {"total": "2000000000000", "used": "500000000000"}}],
    "disks": [{"id": "sata1", "name": "Drive 1", "temp": 36, "status": "normal"},
              {"id": "sata2", "name": "Drive 2", "temp": 41, "status": "normal"}],
}
UTILIZATION = {"cpu": {"user_load": 7, "system_load": 3, "other_load": 2, "1min_load": 40},
               "memory": {"memory_size": 4194304, "real_usage": 45, "avail_real": 2000000}}
INFO = {"model": "DS923+", "ram": 4096, "temperature": 46, "temperature_warn": False, "uptime": 300000, "version_string": "DSM 7.2.1-69057 Update 5"}


class FakeBox:
    """<summary>A DSM 7 answering as the real one does, with a password and a session it may forget.</summary>"""

    def __init__(self, password="pw", otp=False):
        """<summary>No session yet; ``otp`` makes the account one that needs a one time code.</summary>"""
        self.password = password
        self.otp = otp
        self.sids = []
        self.expired = set()
        self.calls = []
        self.utilization_ok = True

    def __call__(self, method, url, params, data, timeout, verify):
        """<summary>Answer one call as the box would.</summary>"""
        self.calls.append((method, url, dict(params), dict(data) if data else None, verify))
        api = params.get("api")
        if url.endswith("/webapi/query.cgi") and api == synology.API_INFO:
            return {"success": True, "data": DISCOVERY}
        assert url.endswith("/webapi/entry.cgi"), url
        if api == synology.API_AUTH and params.get("method") == "login":
            assert method == "POST" and "passwd" not in params  # never in the request line
            if self.otp:
                return {"success": False, "error": {"code": 403}}
            if data.get("passwd") != self.password or data.get("account") != "deck":
                return {"success": False, "error": {"code": 400}}
            sid = f"sid{len(self.sids) + 1}"
            self.sids.append(sid)
            return {"success": True, "data": {"sid": sid}}
        sid = params.get("_sid")
        if sid not in self.sids or sid in self.expired:
            return {"success": False, "error": {"code": 119}}
        if api == synology.API_STORAGE:
            return {"success": True, "data": STORAGE}
        if api == synology.API_UTILIZATION:
            if not self.utilization_ok:
                return {"success": False, "error": {"code": 105}}
            return {"success": True, "data": UTILIZATION}
        if api == synology.API_DSM_INFO:
            return {"success": True, "data": INFO}
        raise AssertionError(api)


READING = SynologyReading((Volume("volume_1", "normal", 8_000_000_000_000, 5_000_000_000_000),
                           Volume("volume_2", "degraded", 2_000_000_000_000, 500_000_000_000)),
                          True, 41, 12, 45, 46, False, 300000, "DS923+")


def test_replies_are_read_as_dsm_sends_them():
    """<summary>Sizes arrive as strings of bytes, the loads add up, the info
    fields come through, a reply that is not DSM's is refused, and the
    reading answers free space for one volume or all of them.</summary>"""
    volumes, disks_ok, disk_temp = synology.parse_storage(STORAGE)
    assert volumes == READING.volumes and disks_ok and disk_temp == 41
    assert synology.parse_storage({"volumes": [{"id": "v", "status": "normal"}], "disks": [{"status": "crashed"}]}) == \
        ((Volume("v", "normal", 0, 0),), False, None)
    with pytest.raises(ValueError):
        synology.parse_storage({"hello": 1})
    assert synology.parse_utilization(UTILIZATION) == (12, 45)
    assert synology.parse_utilization({"cpu": {}}) == (None, None)
    assert synology.parse_info(INFO) == ("DS923+", 46, False, 300000)
    assert synology.parse_info(None) == ("", None, False, None)
    assert READING.space() == (4_500_000_000_000, 10_000_000_000_000)
    assert READING.space("VOLUME_2") == (1_500_000_000_000, 2_000_000_000_000)
    assert READING.space("volume_9") is None and SynologyReading(()).space() is None
    assert READING.healthy is False  # volume_2 is degraded
    assert SynologyReading((Volume("volume_1", "normal", 10, 5),)).healthy is True
    assert SynologyReading((Volume("volume_1", "normal", 10, 5),), temperature_warn=True).healthy is False
    assert synology.api_url("http://nas.test:5000/", "entry.cgi") == "http://nas.test:5000/webapi/entry.cgi"


def test_the_session_discovers_signs_in_and_signs_in_again_when_the_box_forgets_it(tmp_path):
    """<summary>The first call asks where the APIs live, signs in with the
    password from the file as a POST body, then carries the session id as a
    parameter; error 119 later signs in once more and retries; a wrong
    password and a one time code account are refusals in DSM's own words;
    a key with no password file cannot sign in.</summary>"""
    box = FakeBox()
    secret = tmp_path / "synology"
    secret.write_text("pw\n")
    session = SynologySession("http://nas.test:5000/", "deck", str(secret), request=box)
    assert synology.fetch(session) == READING
    names = [(c[0], c[2].get("api"), c[2].get("method")) for c in box.calls]
    assert names == [("GET", synology.API_INFO, "query"), ("POST", synology.API_AUTH, "login"),
                     ("GET", synology.API_STORAGE, "load_info"), ("GET", synology.API_UTILIZATION, "get"),
                     ("GET", synology.API_DSM_INFO, "getinfo")]
    assert box.calls[1][2]["version"] == 7 and box.calls[1][3] == {"account": "deck", "passwd": "pw", "format": "sid"}
    assert box.calls[2][2]["_sid"] == "sid1" and box.calls[4][2]["version"] == 2
    box.expired.add("sid1")
    assert synology.fetch(session).model == "DS923+"
    assert box.sids == ["sid1", "sid2"]
    box.utilization_ok = False
    assert synology.fetch(session).cpu is None and synology.fetch(session).volumes == READING.volumes
    secret.write_text("wrong\n")
    box.expired.add("sid2")
    with pytest.raises(ValueError, match="account or password is wrong"):
        synology.fetch(session)
    with pytest.raises(ValueError, match="one time code"):
        synology.fetch(SynologySession("http://nas.test:5000/", "deck", str(secret), request=FakeBox(otp=True)))
    with pytest.raises(ValueError, match="credentials file"):
        synology.fetch(SynologySession("http://nas.test:5000/", "deck", None, request=box))
    with pytest.raises(ValueError, match="not a DSM box"):
        synology.fetch(SynologySession("http://nas.test:5000/", "deck", str(secret),
                                       request=lambda *a: {"success": True, "data": {}}))
    assert synology.session_for("http://nas.test:5000/", "deck", str(secret), False, request=box) is \
        synology.session_for("http://nas.test:5000/", "deck", str(secret), False, request=box)
    assert synology.session_for("http://nas.test:5000/", "other", str(secret), False, request=box).account == "other"


def test_the_monitor_polls_on_the_interval_and_logs_reachability_once():
    """<summary>A watched box is asked at once and then on its interval, an
    unreachable box reads as None and the change is logged once, and
    unwatching forgets it.</summary>"""
    clock = Clock()
    lines = []
    asked = []

    def prober(url, account, token_file, insecure):
        asked.append((url, account, token_file, insecure))
        if url.startswith("http://gone"):
            raise ConnectionError("refused")
        return READING

    monitor = SynologyMonitor(prober=prober, monotonic=clock, log=lines.append, threaded=False)
    monitor.watch([("http://nas.test:5000/", "deck", "~/s", False, 30_000), ("http://gone.test/", "x", None, True, 5_000),
                   ("http://nas.test:5000/", "deck", "~/s", False, 60_000)])
    assert monitor.state("http://nas.test:5000/") is None
    monitor.poll_once()
    assert asked == [("http://nas.test:5000/", "deck", "~/s", False), ("http://gone.test/", "x", None, True)]
    assert monitor.state("http://nas.test:5000/") == READING and monitor.state("http://gone.test/") is None
    assert len(lines) == 2 and any("unreachable" in line and "refused" in line for line in lines)
    clock.t += 6
    monitor.poll_once()
    assert asked[-1][0] == "http://gone.test/" and len(asked) == 3
    clock.t += 25
    monitor.poll_once()
    assert asked.count(("http://nas.test:5000/", "deck", "~/s", False)) == 2
    assert len(lines) == 2  # still unreachable, said once
    monitor.watch([])
    assert monitor.state("http://nas.test:5000/") is None and monitor.poll_once() == 60.0


def test_a_press_opens_dsm_and_sends_nothing():
    """<summary>The press is the browser's: DSM's address opens, and no call goes to the box.</summary>"""
    opened = []
    runner = ActionRunner(Context(), open_url=opened.append, log=lambda t: None)
    params = {"url": "http://nas.test:5000/", "account": "deck", "token_file": "~/s", "insecure": False,
              "show": "space", "volume": None, "do": "open", "every_ms": 30_000}
    assert runner.run(Action("synology", params))
    assert opened == ["http://nas.test:5000/"]


def test_a_key_with_no_account_yet_is_refused_in_words(tmp_path):
    """<summary>An account left empty, as a key being set up has, is a sign in
    refusal naming the account, not a parse error and not a request.</summary>"""
    secret = tmp_path / "synology"
    secret.write_text("pw\n")
    box = FakeBox()
    with pytest.raises(ValueError, match="names no account"):
        synology.fetch(SynologySession("http://nas.test:5000/", None, str(secret), request=box))
    assert [c[2].get("api") for c in box.calls] == [synology.API_INFO]
