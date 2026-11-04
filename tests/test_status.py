"""<summary>
The status monitor behind the home lab keys, driven by hand with a prober
that answers from a table.
</summary>
<remarks>
No thread is started here: <see cref="StatusMonitor.poll_once"/> is called
directly so the schedule can be checked against a fake clock. The one test
that does start the thread only checks it can be stopped.
</remarks>
"""

from deckplate.status import StatusMonitor, Watch


class Clock:
    """<summary>A monotonic clock moved by hand.</summary>"""

    def __init__(self):
        """<summary>Start at a round number.</summary>"""
        self.t = 1000.0

    def __call__(self):
        """<summary>Read the clock.</summary>"""
        return self.t


def test_watched_addresses_are_asked_at_once_and_then_on_their_interval():
    """<summary>
    A new address is asked on the first poll and not again until its interval
    has passed, and the answer is what the key reads.
    </summary>
    """
    clock = Clock()
    answers = {"http://up.test/": True, "http://down.test/": False}
    asked = []

    def prober(watch):
        asked.append(watch.url)
        return answers[watch.url]

    monitor = StatusMonitor(prober=prober, monotonic=clock, log=lambda t: None, threaded=False)
    monitor.watch([Watch("http://up.test/", every_ms=60_000), Watch("http://down.test/", every_ms=30_000)])
    assert monitor.ok("http://up.test/") is None
    monitor.poll_once()
    assert monitor.ok("http://up.test/") is True
    assert monitor.ok("http://down.test/") is False
    assert sorted(asked) == ["http://down.test/", "http://up.test/"]
    monitor.poll_once()
    assert len(asked) == 2  # nothing due yet
    clock.t += 31
    monitor.poll_once()
    assert asked[-1] == "http://down.test/" and len(asked) == 3
    assert monitor.ok("http://nowhere.test/") is None


def test_a_prober_that_raises_counts_as_down_and_changes_are_logged_once():
    """<summary>
    Connection refused is down, not an error, and the log gets one line per
    change of state rather than one per poll.
    </summary>
    """
    clock = Clock()
    lines = []
    state = {"up": False}

    def prober(watch):
        if not state["up"]:
            raise ConnectionError("refused")
        return True

    monitor = StatusMonitor(prober=prober, monotonic=clock, log=lines.append, threaded=False)
    monitor.watch([Watch("http://box.test/", every_ms=5_000)])
    for _ in range(3):
        monitor.poll_once()
        clock.t += 6
    assert monitor.ok("http://box.test/") is False
    assert len(lines) == 1 and "down" in lines[0] and "refused" in lines[0]
    state["up"] = True
    monitor.poll_once()
    assert monitor.ok("http://box.test/") is True
    assert len(lines) == 2 and "up" in lines[1]


def test_replacing_the_watches_drops_the_old_answers():
    """<summary>
    A reload that removes a status key forgets its address, and one that
    adds a key asks it at once.
    </summary>
    """
    clock = Clock()
    monitor = StatusMonitor(prober=lambda w: True, monotonic=clock, log=lambda t: None, threaded=False)
    monitor.watch([Watch("http://a.test/")])
    monitor.poll_once()
    assert monitor.ok("http://a.test/") is True
    monitor.watch([Watch("http://b.test/")])
    assert monitor.ok("http://a.test/") is None
    assert monitor.ok("http://b.test/") is None
    monitor.poll_once()
    assert monitor.ok("http://b.test/") is True


def test_the_polling_thread_starts_with_the_first_watch_and_stops_cleanly():
    """<summary>The background thread runs once something is watched and joins on stop.</summary>"""
    import time
    seen = []
    monitor = StatusMonitor(prober=lambda w: seen.append(w.url) or True, log=lambda t: None)
    monitor.watch([Watch("http://a.test/", every_ms=5_000)])
    deadline = time.monotonic() + 2
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == ["http://a.test/"]
    monitor.stop()
    assert not monitor._thread.is_alive()


def test_a_watch_with_a_credentials_file_sends_it_and_a_bare_one_does_not(monkeypatch, tmp_path):
    """<summary>
    The probe reads the watch's credentials file on every ask and sends it
    as the Authorization header, exactly as a press would, and a watch
    without one sends no such header.
    </summary>
    """
    from deckplate.status import probe
    token = tmp_path / "kodi-other"
    token.write_text("Basic abc123\n", encoding="utf-8")
    sent = []

    class Reply:
        """<summary>A 200 with nothing in it.</summary>"""
        status_code = 200
        text = ""

    def fake_get(url, timeout, verify, headers):
        sent.append((url, headers.get("Authorization")))
        return Reply()

    monkeypatch.setattr("deckplate.status.requests.get", fake_get)
    assert probe(Watch("http://player.test/jsonrpc", token_file=str(token))) is True
    assert probe(Watch("http://plain.test/")) is True
    assert sent == [("http://player.test/jsonrpc", "Basic abc123"), ("http://plain.test/", None)]
