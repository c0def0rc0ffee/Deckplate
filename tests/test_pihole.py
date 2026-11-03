"""<summary>
Pi-hole: the reading its replies become, the session that signs in when the
box asks, the pause a press sends, the monitor driven by hand, and the
runner's press through a recording request door.
</summary>
"""

import pytest

from deckplate import pihole
from deckplate.actions import ActionRunner
from deckplate.config import Action
from deckplate.pihole import PiholeMonitor, PiholeReading, PiholeSession


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


SUMMARY = {"queries": {"total": 12345, "blocked": 2345, "percent_blocked": 19.0}, "gravity": {"domains_being_blocked": 150000}}
ON = {"blocking": "enabled", "timer": None}
OFF = {"blocking": "disabled", "timer": 299.6}


class FakeBox:
    """<summary>A Pi-hole 6 answering as the real one does: 401 without a session, a sign in, then the calls.</summary>"""

    def __init__(self, password="pw"):
        """<summary>Blocking on, no session yet.</summary>"""
        self.password = password
        self.sids = []
        self.calls = []
        self.blocking = dict(ON)
        self.expired = set()

    def __call__(self, method, url, headers, body, timeout, verify):
        """<summary>Answer one call as the box would.</summary>"""
        self.calls.append((method, url, dict(headers), body, verify))
        if url.endswith("/api/auth") and method == "POST":
            if body.get("password") != self.password:
                return 401, {"session": {"valid": False, "sid": None, "message": "password incorrect"}}
            sid = f"sid{len(self.sids) + 1}"
            self.sids.append(sid)
            return 200, {"session": {"valid": True, "sid": sid, "validity": 300}}
        sid = headers.get("sid")
        if sid not in self.sids or sid in self.expired:
            return 401, {"error": {"key": "unauthorized", "message": "Unauthorized"}}
        if url.endswith("/api/stats/summary"):
            return 200, SUMMARY
        if url.endswith("/api/dns/blocking") and method == "GET":
            return 200, self.blocking
        if url.endswith("/api/dns/blocking") and method == "POST":
            self.blocking = {"blocking": "enabled" if body["blocking"] else "disabled", "timer": body["timer"]}
            return 200, self.blocking
        raise AssertionError(url)


def test_addresses_and_replies_are_read_as_pi_hole_sends_them():
    """<summary>The API sits under the root whether the key wrote the root,
    a slash or the admin page; the summary and blocking replies become one
    reading; and a reply that is not Pi-hole's is refused.</summary>"""
    assert pihole.api_url("http://pi.hole", "/auth") == "http://pi.hole/api/auth"
    assert pihole.api_url("http://pi.hole/admin/", "/auth") == "http://pi.hole/api/auth"
    assert pihole.admin_url("http://pi.hole") == "http://pi.hole/admin/"
    reading = pihole.parse_summary(SUMMARY, ON)
    assert reading == PiholeReading(True, None, 12345, 2345, 19.0, 150000)
    assert pihole.parse_summary(SUMMARY, OFF).timer == 300 and pihole.parse_summary(SUMMARY, OFF).blocking is False
    assert pihole.parse_summary({"queries": {"total": 10, "blocked": 5}}, ON).percent == 50.0
    with pytest.raises(ValueError):
        pihole.parse_summary({"hello": 1}, ON)
    with pytest.raises(ValueError):
        pihole.parse_blocking({"blocking": "maybe"})


def test_the_session_signs_in_on_demand_and_again_when_the_box_forgets_it(tmp_path):
    """<summary>The first call signs in with the password from the file and
    carries the session id afterwards; a 401 on a later call signs in once
    more and retries; a wrong password is a refusal, not a loop; a key
    with no password file cannot sign in.</summary>"""
    box = FakeBox()
    secret = tmp_path / "pihole"
    secret.write_text("pw\n")
    session = PiholeSession("http://pi.hole/", str(secret), request=box)
    assert pihole.fetch(session) == PiholeReading(True, None, 12345, 2345, 19.0, 150000)
    assert [c[0] + " " + c[1].rsplit("/api", 1)[1] for c in box.calls] == \
        ["GET /stats/summary", "POST /auth", "GET /stats/summary", "GET /dns/blocking"]
    assert box.calls[-1][2] == {"sid": "sid1"}
    box.expired.add("sid1")
    assert pihole.set_blocking(session, False, 120) == (False, 120)
    assert box.sids == ["sid1", "sid2"] and box.calls[-1][3] == {"blocking": False, "timer": 120}
    assert pihole.set_blocking(session, True, 120) == (True, None)
    secret.write_text("wrong\n")
    box.expired.add("sid2")
    with pytest.raises(ValueError, match="refused the password"):
        pihole.fetch(session)
    bare = PiholeSession("http://pi.hole/", None, request=box)
    with pytest.raises(ValueError, match="credentials file"):
        pihole.fetch(bare)
    assert pihole.session_for("http://pi.hole/", str(secret), False, request=box) is \
        pihole.session_for("http://pi.hole/", str(secret), False, request=box)
    assert pihole.session_for("http://pi.hole/", str(secret), True, request=box).insecure is True


def test_the_monitor_polls_on_the_interval_and_a_press_updates_it_at_once():
    """<summary>A watched box is asked at once and then on its interval; an
    update from a press changes the blocking state and pushes the next poll
    out; an unreachable box reads as None and the change is logged once.</summary>"""
    clock = Clock()
    lines = []
    answers = {"http://pi.hole/": PiholeReading(True, None, 10, 2, 20.0, 5)}
    asked = []

    def prober(url, token_file, insecure):
        asked.append((url, token_file, insecure))
        if url not in answers:
            raise ConnectionError("refused")
        return answers[url]

    monitor = PiholeMonitor(prober=prober, monotonic=clock, log=lines.append, threaded=False)
    monitor.watch([("http://pi.hole/", "~/p", False, 10_000), ("http://gone.test/", None, True, 5_000), ("http://pi.hole/", "~/p", False, 60_000)])
    assert monitor.state("http://pi.hole/") is None
    monitor.poll_once()
    assert asked == [("http://pi.hole/", "~/p", False), ("http://gone.test/", None, True)]
    assert monitor.state("http://pi.hole/") == answers["http://pi.hole/"]
    assert monitor.state("http://gone.test/") is None
    assert len(lines) == 2 and any("unreachable" in line and "refused" in line for line in lines)
    monitor.update("http://pi.hole/", False, 300)
    assert monitor.state("http://pi.hole/") == PiholeReading(False, 300, 10, 2, 20.0, 5)
    clock.t += 6
    monitor.poll_once()
    assert asked[-1][0] == "http://gone.test/" and len(asked) == 3
    clock.t += 5
    monitor.poll_once()
    assert asked.count(("http://pi.hole/", "~/p", False)) == 2  # the update pushed its poll past 10 s
    monitor.watch([])
    assert monitor.state("http://pi.hole/") is None and monitor.poll_once() == 60.0


def test_a_press_pauses_or_resumes_by_the_latest_reading_and_feeds_the_monitor(tmp_path):
    """<summary>With blocking on a press pauses for the key's seconds and the
    monitor's state follows; the next press resumes; with no reading yet the
    box is asked first; an open key opens the admin page and sends nothing.</summary>"""
    box = FakeBox()
    secret = tmp_path / "pihole"
    secret.write_text("pw\n")
    pihole.session_for("http://pi.hole/", str(secret), False, request=box)
    monitor = PiholeMonitor(prober=lambda u, t, i: PiholeReading(True, None, 10, 2, 20.0, 5), threaded=False)
    monitor.watch([("http://pi.hole/", str(secret), False, 10_000)])
    monitor.poll_once()
    opened = []
    runner = ActionRunner(Context(), open_url=opened.append, log=lambda t: None)
    runner.pihole = monitor
    params = {"url": "http://pi.hole/", "token_file": str(secret), "insecure": False, "do": "toggle", "seconds": 120, "every_ms": 10_000}
    assert runner.run(Action("pihole", params))
    assert box.blocking == {"blocking": "disabled", "timer": 120}
    assert monitor.state("http://pi.hole/") == PiholeReading(False, 120, 10, 2, 20.0, 5)
    assert runner.run(Action("pihole", params))
    assert box.blocking == {"blocking": "enabled", "timer": None} and monitor.state("http://pi.hole/").blocking is True
    monitor.watch([])
    before = len(box.calls)
    assert runner.run(Action("pihole", params))
    assert box.calls[before][1].endswith("/api/stats/summary") and box.blocking["blocking"] == "disabled"
    assert runner.run(Action("pihole", dict(params, do="open")))
    assert opened == ["http://pi.hole/admin/"]
