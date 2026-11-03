"""<summary>
The now playing reading and its service, with the session bus replaced by
canned answers.
</summary>
"""

from deckplate import media, sessionbus


class Clock:
    """<summary>A monotonic clock moved by hand.</summary>"""

    def __init__(self):
        """<summary>Start at zero.</summary>"""
        self.t = 0.0

    def __call__(self):
        """<summary>Read the clock.</summary>"""
        return self.t


def test_fetch_prefers_the_playing_player_and_skips_one_that_fails(monkeypatch):
    """<summary>
    With two players open the one that is playing is shown, a player that
    fails to answer is skipped, and the artist list's first entry is used.
    </summary>
    """
    names = ["org.freedesktop.DBus", "org.mpris.MediaPlayer2.dead", "org.mpris.MediaPlayer2.vlc",
             "org.mpris.MediaPlayer2.spotify"]
    props = {
        "org.mpris.MediaPlayer2.vlc": {"PlaybackStatus": "Paused",
                                       "Metadata": {"xesam:title": "Quiet one", "xesam:artist": ["A"]}},
        "org.mpris.MediaPlayer2.spotify": {"PlaybackStatus": "Playing",
                                           "Metadata": {"xesam:title": "Loud one", "xesam:artist": ["B", "C"]}},
    }

    def get_property(name, path, interface, prop):
        if name not in props:
            raise sessionbus.BusError("gone")
        return props[name][prop]

    monkeypatch.setattr(media.sessionbus, "list_names", lambda: names)
    monkeypatch.setattr(media.sessionbus, "get_property", get_property)
    reading = media.fetch()
    assert reading == media.NowPlaying("Loud one", "B", "Playing", "spotify")
    assert reading.playing
    props["org.mpris.MediaPlayer2.spotify"]["PlaybackStatus"] = "Stopped"
    assert media.fetch().player == "vlc"
    monkeypatch.setattr(media.sessionbus, "list_names", lambda: ["org.freedesktop.DBus"])
    assert media.fetch() is None


def test_service_refreshes_on_its_interval_and_clears_on_failure():
    """<summary>
    The service reads only when due, reports a change, and a bus failure
    leaves the panel empty with one log line rather than a stale track.
    </summary>
    """
    clock = Clock()
    lines = []
    answers = [media.NowPlaying("One", "", "Playing", "p"), media.NowPlaying("One", "", "Playing", "p"),
               RuntimeError("bus gone"), RuntimeError("bus gone")]

    def fetcher():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    service = media.MediaService(fetcher=fetcher, monotonic=clock, log=lines.append, interval=2)
    assert service.refresh_if_due() is True
    assert service.reading.title == "One"
    assert service.refresh_if_due() is False  # not due
    clock.t = 2.5
    assert service.refresh_if_due() is False  # same reading
    clock.t = 5
    assert service.refresh_if_due() is True and service.reading is None
    clock.t = 8
    service.refresh_if_due()
    assert len(lines) == 1 and "unavailable" in lines[0]


# ---- A Kodi player as the source -------------------------------------------

PLAYERS = {"result": [{"playerid": 1, "type": "video"}]}
NO_PLAYERS = {"result": []}


def test_kodi_reading_builds_the_track_from_the_three_replies():
    """<summary>
    Music gets its artist, television its show with season and episode, a
    film its title alone, a paused player reads as paused, and no active
    player reads as nothing playing.
    </summary>
    """
    music = {"result": {"item": {"label": "x", "title": "Song", "artist": ["Band", "Other"]}}}
    assert media.kodi_reading(PLAYERS, music, {"result": {"speed": 1}}) == media.NowPlaying("Song", "Band", "Playing", "kodi")
    episode = {"result": {"item": {"label": "Ep", "title": "Pilot", "artist": [], "showtitle": "Show", "season": 2, "episode": 3}}}
    assert media.kodi_reading(PLAYERS, episode, {"result": {"speed": 0}}) == media.NowPlaying("Pilot", "Show 2x03", "Paused", "kodi")
    film = {"result": {"item": {"label": "Film.mkv", "title": "", "artist": [], "showtitle": "", "season": -1, "episode": -1}}}
    assert media.kodi_reading(PLAYERS, film, {"result": {"speed": 1}}) == media.NowPlaying("Film.mkv", "", "Playing", "kodi")
    assert media.kodi_reading(NO_PLAYERS, music, {"result": {"speed": 1}}) is None
    assert media.kodi_reading(PLAYERS, {"error": "no"}, {"error": "no"}) == media.NowPlaying("", "", "Playing", "kodi")


def test_fetch_kodi_sends_the_three_requests_with_the_credentials():
    """<summary>
    The active players are asked first with the credentials file's header,
    then the item and the speed of the first; with no player the other two
    are never sent, and no credentials file means no header.
    </summary>
    """
    sent = []
    replies = iter([PLAYERS, {"result": {"item": {"title": "Song", "artist": ["Band"]}}}, {"result": {"speed": 1}}])

    def send_json(url, headers, body, timeout):
        sent.append((url, headers, body, timeout))
        return next(replies)

    reading = media.fetch_kodi("http://p.test/jsonrpc", "~/t", send_json, lambda path: "Basic abc", 4.0)
    assert reading == media.NowPlaying("Song", "Band", "Playing", "kodi")
    assert [body["method"] for _url, _headers, body, _t in sent] == ["Player.GetActivePlayers", "Player.GetItem", "Player.GetProperties"]
    assert all(url == "http://p.test/jsonrpc" and headers == {"Authorization": "Basic abc"} and timeout == 4.0
               for url, headers, _body, timeout in sent)
    assert sent[1][2]["params"] == {"playerid": 1, "properties": media.KODI_ITEM_PROPERTIES}
    assert sent[2][2]["params"] == {"playerid": 1, "properties": ["speed"]}
    sent.clear()
    assert media.fetch_kodi("http://p.test/jsonrpc", None, lambda *a: NO_PLAYERS, lambda path: "never", 4.0) is None
    recorded = []
    media.fetch_kodi("http://p.test/jsonrpc", None, lambda url, headers, body, t: recorded.append(headers) or NO_PLAYERS,
                     lambda path: "never", 4.0)
    assert recorded == [{}]


def test_the_kodi_monitor_polls_on_its_interval_and_logs_reachability_once():
    """<summary>
    A watched player is asked at once and then on the interval; a player
    that fails reads as nothing playing and the change is logged once each
    way; dropping the player drops its reading.
    </summary>
    """
    clock = Clock()
    lines = []
    answers = {"http://p.test/jsonrpc": media.NowPlaying("Song", "Band", "Playing", "kodi")}
    asked = []

    def prober(url, token_file):
        asked.append((url, token_file))
        if url not in answers:
            raise ConnectionError("refused")
        return answers[url]

    monitor = media.KodiPlayingMonitor(prober=prober, monotonic=clock, log=lines.append, threaded=False, every_ms=3000)
    monitor.watch([("http://p.test/jsonrpc", "~/t"), ("http://gone.test/jsonrpc", None), ("http://p.test/jsonrpc", "~/other")])
    assert monitor.reading("http://p.test/jsonrpc") is None
    monitor.poll_once()
    assert asked == [("http://p.test/jsonrpc", "~/t"), ("http://gone.test/jsonrpc", None)]
    assert monitor.reading("http://p.test/jsonrpc") == answers["http://p.test/jsonrpc"]
    assert monitor.reading("http://gone.test/jsonrpc") is None
    assert lines == ["now playing http://gone.test/jsonrpc: unreachable (refused)"]
    monitor.poll_once()
    assert len(asked) == 2
    clock.t += 3.1
    monitor.poll_once()
    assert len(asked) == 4 and len(lines) == 1
    answers["http://gone.test/jsonrpc"] = None
    clock.t += 3.1
    monitor.poll_once()
    assert lines[-1] == "now playing http://gone.test/jsonrpc: reachable"
    monitor.watch([])
    assert monitor.reading("http://p.test/jsonrpc") is None
    assert monitor.poll_once() == 60.0


# ---- The picture of what is playing ------------------------------------------

def test_readings_carry_the_picture_address(monkeypatch):
    """<summary>
    A desktop player's art address rides on its reading, and a Kodi item's
    picture becomes an address on the player's own web server, escaped,
    with the player's credentials file beside it; an item with no picture
    carries none.
    </summary>
    """
    props = {"PlaybackStatus": "Playing", "Metadata": {"xesam:title": "T", "mpris:artUrl": "file:///tmp/cover.jpg"}}
    monkeypatch.setattr(media.sessionbus, "get_property", lambda name, path, interface, prop: props[prop])
    assert media.read_player("org.mpris.MediaPlayer2.vlc").art == "file:///tmp/cover.jpg"
    assert media.kodi_image_url("http://p.test:8080/jsonrpc", "image://smb%3a%2f%2fbox%2fa%20b.jpg/") \
        == "http://p.test:8080/image/image%3A%2F%2Fsmb%253a%252f%252fbox%252fa%2520b.jpg%2F"
    item = {"result": {"item": {"title": "Song", "artist": ["Band"], "thumbnail": "image://t/",
                                "art": {"poster": "image://p/", "thumb": "image://thumb/"}}}}
    reading = media.kodi_reading(PLAYERS, item, {"result": {"speed": 1}}, "http://p.test:8080/jsonrpc", "~/t")
    assert reading.art == "http://p.test:8080/image/image%3A%2F%2Fthumb%2F" and reading.art_token_file == "~/t"
    bare = {"result": {"item": {"title": "Song", "artist": ["Band"], "thumbnail": "", "art": {}}}}
    reading = media.kodi_reading(PLAYERS, bare, {"result": {"speed": 1}}, "http://p.test:8080/jsonrpc", "~/t")
    assert reading.art is None and reading.art_token_file is None
    assert media.kodi_reading(PLAYERS, item, {"result": {"speed": 1}}).art is None


def test_the_art_cache_fetches_once_remembers_failures_and_stays_bounded():
    """<summary>
    The first ask queues a fetch and answers None, the fetch fills the
    cache, a second ask is answered from it; a failed address is logged
    and not asked again until the retry time passes; and the oldest
    pictures go once the limit is passed.
    </summary>
    """
    from PIL import Image
    clock = Clock()
    lines = []
    fetched = []

    def fetcher(url, token_file):
        fetched.append((url, token_file))
        if "bad" in url:
            raise ConnectionError("refused")
        return Image.new("RGB", (4, 4), (255, 0, 0))

    cache = media.ArtCache(fetcher=fetcher, monotonic=clock, log=lines.append, threaded=False, limit=2, retry_s=100)
    assert cache.image("http://a.test/1.jpg", "~/t") is None
    assert cache.image("http://a.test/1.jpg", "~/t") is None
    assert cache.fetch_once() is True
    assert fetched == [("http://a.test/1.jpg", "~/t")]
    assert cache.image("http://a.test/1.jpg").size == (4, 4)
    assert cache.fetch_once() is False
    assert cache.image("http://bad.test/x.jpg") is None
    cache.fetch_once()
    assert lines == ["now playing picture http://bad.test/x.jpg: refused"]
    assert cache.image("http://bad.test/x.jpg") is None and cache.fetch_once() is False
    clock.t += 101
    assert cache.image("http://bad.test/x.jpg") is None and cache.fetch_once() is True
    for n in (2, 3):
        cache.image(f"http://a.test/{n}.jpg")
    cache.fetch_once()
    assert cache.image("http://a.test/1.jpg") is None  # evicted, so asked again
    assert cache.image("http://a.test/3.jpg") is not None
