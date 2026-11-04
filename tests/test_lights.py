"""<summary>
WLED lights: the state a reply becomes, the body a press sends, the monitor
driven by hand, and the runner's press through a recording JSON door.
</summary>
"""

import pytest

from deckplate import lights
from deckplate.actions import ActionRunner
from deckplate.config import Action
from deckplate.lights import LightState, LightsMonitor


class Context:
    """<summary>A deck context that does nothing, for a runner under test.</summary>"""

    def switch_page(self, target):
        """<summary>Ignored.</summary>"""

    def set_brightness(self, value=None, delta=None):
        """<summary>Ignored.</summary>"""

    def sleep_deck(self):
        """<summary>Ignored.</summary>"""


def test_a_state_reply_is_read_and_a_bad_one_is_refused():
    """<summary>on, bri, ps and the first segment colour come through; missing parts default.</summary>"""
    state = lights.parse_state({"on": True, "bri": 128, "ps": 2, "seg": [{"col": [[255, 160, 0], [0, 0, 0]]}]})
    assert state == LightState(True, 128, (255, 160, 0), 2)
    assert state.percent == 50
    assert lights.parse_state({"on": False}) == LightState(False, 0, (0, 0, 0), -1)
    assert lights.parse_state({"on": True, "bri": 999, "seg": []}).brightness == 255
    with pytest.raises(ValueError):
        lights.parse_state({"success": True})
    assert lights.state_url("http://lamp.test/") == "http://lamp.test/json/state"
    assert lights.state_url("http://lamp.test") == "http://lamp.test/json/state"
    assert lights.state_url("http://lamp.test/json/state") == "http://lamp.test/json/state"


def test_each_doing_becomes_the_body_wled_wants():
    """<summary>toggle, on, off, a preset and the brightness steps, every one asking for the state back.</summary>"""
    assert lights.command({"do": "toggle"}, None) == {"v": True, "on": "t"}
    assert lights.command({"do": "on"}, None) == {"v": True, "on": True}
    assert lights.command({"do": "off"}, None) == {"v": True, "on": False}
    assert lights.command({"do": "preset", "preset": 4}, None) == {"v": True, "on": True, "ps": 4}
    lit = LightState(True, 100, (1, 2, 3))
    assert lights.command({"do": "brighter", "step": 32}, lit) == {"v": True, "on": True, "bri": 132}
    assert lights.command({"do": "dimmer", "step": 32}, lit) == {"v": True, "on": True, "bri": 68}
    assert lights.command({"do": "dimmer", "step": 200}, lit)["bri"] == 1
    assert lights.command({"do": "brighter", "step": 200}, lit)["bri"] == 255
    assert lights.command({"do": "brighter"}, None)["bri"] == 127 + 32
    with pytest.raises(ValueError):
        lights.command({"do": "disco"}, None)


class Clock:
    """<summary>A monotonic clock moved by hand.</summary>"""

    def __init__(self):
        """<summary>Start at a round number.</summary>"""
        self.t = 1000.0

    def __call__(self):
        """<summary>Read the clock.</summary>"""
        return self.t


def test_the_monitor_polls_on_the_interval_and_a_press_updates_it_at_once():
    """<summary>
    A watched device is asked at once and then on its interval; an update
    from a press replaces the state and pushes the next poll out; an
    unreachable device reads as None and the change is logged once.
    </summary>
    """
    clock = Clock()
    lines = []
    answers = {"http://lamp.test/": LightState(True, 128, (255, 160, 0))}
    asked = []

    def prober(url):
        asked.append(url)
        if url not in answers:
            raise ConnectionError("refused")
        return answers[url]

    monitor = LightsMonitor(prober=prober, monotonic=clock, log=lines.append, threaded=False)
    monitor.watch([("http://lamp.test/", 10_000), ("http://gone.test/", 5_000), ("http://lamp.test/", 60_000)])
    assert monitor.state("http://lamp.test/") is None
    monitor.poll_once()
    assert monitor.state("http://lamp.test/") == LightState(True, 128, (255, 160, 0))
    assert monitor.state("http://gone.test/") is None
    assert len(lines) == 2 and any("unreachable" in line and "refused" in line for line in lines)
    assert any(line.endswith(": reachable") for line in lines)
    monitor.poll_once()
    assert len(asked) == 2
    monitor.update("http://lamp.test/", LightState(False, 128, (255, 160, 0)))
    assert monitor.state("http://lamp.test/").on is False
    clock.t += 6
    monitor.poll_once()
    assert asked[-1] == "http://gone.test/" and len(asked) == 3
    clock.t += 5
    monitor.poll_once()
    assert asked.count("http://lamp.test/") == 2  # the update pushed its poll past 10 s
    monitor.watch([])
    assert monitor.state("http://lamp.test/") is None


def test_a_press_posts_the_change_and_feeds_the_reply_to_the_monitor():
    """<summary>The runner posts to the state endpoint and the key's state follows the reply.</summary>"""
    sent = []
    reply = {"on": False, "bri": 128, "seg": [{"col": [[255, 160, 0]]}]}

    def send_json(url, headers, body, timeout):
        sent.append((url, body))
        return reply

    monitor = LightsMonitor(prober=lambda u: LightState(True, 128, (255, 160, 0)), threaded=False)
    monitor.watch([("http://lamp.test/", 10_000)])
    monitor.poll_once()
    runner = ActionRunner(Context(), send_json=send_json, lights=monitor, log=lambda t: None)
    assert runner.run(Action("wled", {"url": "http://lamp.test/", "do": "dimmer", "step": 32}))
    assert sent == [("http://lamp.test/json/state", {"v": True, "on": True, "bri": 96})]
    assert monitor.state("http://lamp.test/") == LightState(False, 128, (255, 160, 0))
    reply = {"success": True}
    assert runner.run(Action("wled", {"url": "http://lamp.test/", "do": "toggle"}))
    assert monitor.state("http://lamp.test/").on is False  # a bare success leaves the state alone
