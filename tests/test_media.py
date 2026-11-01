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
