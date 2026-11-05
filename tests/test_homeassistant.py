"""<summary>
Home Assistant as a key sees it: the state answer read, the token sent, the
monitor polled, and a press through a recording request door.
</summary>
"""

from datetime import datetime, timedelta, timezone

import pytest

from deckplate import homeassistant as ha
from deckplate.actions import ActionRunner
from deckplate.config import Action
from deckplate.homeassistant import HomeAssistantMonitor, HomeAssistantReading, HomeAssistantServer


class Context:
    """<summary>A deck context that does nothing, for a runner under test.</summary>"""

    def switch_page(self, target):
        """<summary>Ignored.</summary>"""

    def set_brightness(self, value=None, delta=None):
        """<summary>Ignored.</summary>"""

    def sleep_deck(self):
        """<summary>Ignored.</summary>"""


class FakeServer:
    """<summary>A request door that records every call and answers like Home Assistant.</summary>"""

    def __init__(self):
        """<summary>A lounge light that is on and a temperature sensor.</summary>"""
        self.calls = []
        self.states = {"light.lounge": "on", "sensor.office": "21.4"}

    def __call__(self, method, url, headers, body, timeout, verify):
        """<summary>Record the call and answer the state or the service.</summary>"""
        self.calls.append((method, url, headers.get("Authorization"), body, verify))
        if method == "GET" and "/api/states/" in url:
            entity = url.rsplit("/", 1)[1]
            if entity not in self.states:
                raise ValueError("404 not found")
            return 200, self._state(entity)
        if method == "POST" and "/api/services/" in url:
            domain, service = url.rsplit("/", 2)[1:]
            entity = body["entity_id"]
            if service in ("toggle", "turn_on", "turn_off"):
                now = {"toggle": "off" if self.states[entity] == "on" else "on", "turn_on": "on", "turn_off": "off"}[service]
                self.states[entity] = now
            return 200, [self._state(entity)]
        raise AssertionError(url)

    def _state(self, entity):
        """<summary>The JSON Home Assistant gives for one entity.</summary>"""
        attributes = {"friendly_name": entity.split(".")[1].capitalize()}
        if entity.startswith("sensor."):
            attributes["unit_of_measurement"] = "°C"
        return {"entity_id": entity, "state": self.states[entity], "attributes": attributes}


def test_a_state_answer_is_read_with_its_words_and_figures():
    """<summary>A light reads as on or off with a word for the face; a sensor reads as a figure with its unit and no lamp.</summary>"""
    light = ha.parse_state({"entity_id": "light.lounge", "state": "on", "attributes": {"friendly_name": "Lounge"}})
    assert light.on is True and light.text == "On" and light.name == "Lounge" and light.domain == "light"
    sensor = ha.parse_state({"entity_id": "sensor.office", "state": "21.4", "attributes": {"unit_of_measurement": "°C"}})
    assert sensor.on is None and sensor.text == "21.4 °C" and sensor.name == "sensor.office"
    assert ha.parse_state({"entity_id": "person.rob", "state": "not_home", "attributes": {}}).text == "Not home"
    with pytest.raises(ValueError):
        ha.parse_state({"message": "Entity not found."})
    assert ha.api_url("http://ha.test:8123/", "/states/light.a") == "http://ha.test:8123/api/states/light.a"


def test_the_token_goes_as_a_bearer_header_and_is_read_from_its_file_each_time(tmp_path):
    """<summary>Every call carries the file's token; a missing or empty file is a clear error, not a call.</summary>"""
    box = FakeServer()
    secret = tmp_path / "ha"
    secret.write_text("abc.def\n")
    server = HomeAssistantServer("http://ha.test/", str(secret), request=box)
    reading = ha.fetch(server, "light.lounge")
    assert reading.state == "on"
    assert box.calls[-1][:3] == ("GET", "http://ha.test/api/states/light.lounge", "Bearer abc.def")
    secret.write_text("")
    with pytest.raises(ValueError, match="empty"):
        ha.fetch(server, "light.lounge")
    with pytest.raises(ValueError, match="token file"):
        ha.fetch(HomeAssistantServer("http://ha.test/", None, request=box), "light.lounge")
    assert ha.parse_data(None) == {} and ha.parse_data('{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        ha.parse_data("[1]")
    with pytest.raises(ValueError, match="domain.service"):
        ha.call_service(server, "toggle")


def test_the_monitor_polls_each_entity_and_a_press_updates_it_at_once():
    """<summary>Entities are keyed by server and id, polled on their interval, and a press's answer lands without waiting.</summary>"""
    clock = [0.0]
    asked = []

    def prober(url, token_file, insecure, entity):
        asked.append(entity)
        if entity == "light.gone":
            raise ValueError("404")
        return HomeAssistantReading(entity, "on" if entity.startswith("light") else "20", entity)
    monitor = HomeAssistantMonitor(prober=prober, monotonic=lambda: clock[0], log=lambda t: None, threaded=False)
    monitor.watch([("http://ha.test/", "~/t", False, "light.lounge", 10_000), ("http://ha.test/", "~/t", False, "sensor.office", 30_000),
                   ("http://ha.test/", "~/t", False, "light.gone", 10_000)])
    assert monitor.state("http://ha.test/", "light.lounge") is None
    monitor.poll_once()
    assert sorted(asked) == ["light.gone", "light.lounge", "sensor.office"]
    assert monitor.state("http://ha.test/", "light.lounge").on is True
    assert monitor.state("http://ha.test/", "light.gone") is None
    asked.clear()
    clock[0] = 11
    monitor.poll_once()
    assert sorted(asked) == ["light.gone", "light.lounge"]
    monitor.update("http://ha.test/", HomeAssistantReading("light.lounge", "off", "Lounge"))
    assert monitor.state("http://ha.test/", "light.lounge").on is False
    monitor.update("http://ha.test/", HomeAssistantReading("light.other", "off", "Other"))
    assert monitor.state("http://ha.test/", "light.other") is None
    monitor.watch([])
    assert monitor.state("http://ha.test/", "light.lounge") is None


def test_a_press_toggles_calls_a_service_or_opens_and_feeds_the_monitor(tmp_path):
    """<summary>Toggle, on and off go through the homeassistant domain with the
    entity; a service of the key's own gets its data and the entity; the
    server's answer updates the monitor; open sends nothing.</summary>"""
    box = FakeServer()
    secret = tmp_path / "ha"
    secret.write_text("tok")
    ha.server_for("http://ha.test/", str(secret), False, request=box)
    monitor = HomeAssistantMonitor(prober=lambda u, t, i, e: HomeAssistantReading(e, "on", e), threaded=False)
    monitor.watch([("http://ha.test/", str(secret), False, "light.lounge", 10_000)])
    monitor.poll_once()
    opened = []
    runner = ActionRunner(Context(), open_url=opened.append, log=lambda t: None)
    runner.homeassistant = monitor
    params = {"url": "http://ha.test/", "token_file": str(secret), "insecure": False, "entity": "light.lounge",
              "do": "toggle", "service": None, "data": None, "every_ms": 10_000}
    assert runner.run(Action("homeassistant", params))
    assert box.calls[-1][:4] == ("POST", "http://ha.test/api/services/homeassistant/toggle", "Bearer tok", {"entity_id": "light.lounge"})
    assert monitor.state("http://ha.test/", "light.lounge").on is False
    assert runner.run(Action("homeassistant", dict(params, do="turn_on")))
    assert box.calls[-1][1].endswith("/homeassistant/turn_on") and monitor.state("http://ha.test/", "light.lounge").on is True
    assert runner.run(Action("homeassistant", dict(params, do="service", service="light.turn_off", data='{"transition": 2}')))
    assert box.calls[-1][1].endswith("/api/services/light/turn_off")
    assert box.calls[-1][3] == {"transition": 2, "entity_id": "light.lounge"}
    assert runner.run(Action("homeassistant", dict(params, do="open")))
    assert opened == ["http://ha.test/"] and box.calls[-1][1].endswith("/light/turn_off")
    assert not runner.run(Action("homeassistant", dict(params, entity=None)))


def test_a_timestamp_reads_as_the_time_coming_or_the_age_gone():
    """<summary>A coming time is its clock time, weekday or date by how far
    off it is; a past one is its age; only an entity marked timestamp is
    read this way, and one with no value keeps its word.</summary>"""
    now = datetime(2026, 11, 4, 20, 0)

    def at(**gap):
        """<summary>A timestamp reading the given gap from now.</summary>"""
        state = (now + timedelta(**gap)).astimezone().isoformat()
        return ha.parse_state({"entity_id": "sensor.t", "state": state, "attributes": {"device_class": "timestamp"}})
    assert at(hours=11, minutes=5).words(now) == "07:05"
    assert at(days=2).words(now) == "Fri 20:00"
    assert at(days=9).words(now) == "13 Nov"
    assert at(seconds=-20).words(now) == "just now"
    assert at(minutes=-5, seconds=-30).words(now) == "5 min ago"
    assert at(hours=-3).words(now) == "3 h ago"
    assert at(days=-2).words(now) == "2 d ago"
    assert at(days=-40).words(now) == "25 Sep"
    assert at(hours=1).on is None
    zulu = (now - timedelta(minutes=5)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    assert ha.parse_state({"entity_id": "sensor.t", "state": zulu, "attributes": {"device_class": "timestamp"}}).words(now) == "5 min ago"
    plain = ha.parse_state({"entity_id": "sensor.t", "state": "2026-11-05T07:05:00+00:00", "attributes": {}})
    assert plain.moment is None and plain.words(now) == plain.text
    empty = ha.parse_state({"entity_id": "sensor.t", "state": "unavailable", "attributes": {"device_class": "timestamp"}})
    assert empty.moment is None and empty.words(now) == "Unavailable"
