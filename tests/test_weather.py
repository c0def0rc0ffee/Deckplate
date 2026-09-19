"""<summary>
Reading the forecast, and the caching service that keeps it off the network.
</summary>
<remarks>
Nothing here reaches the internet. The clock is a fake counter and the fetcher
is a function passed in, so refresh timing can be walked through in a few lines
and a failure can be made to happen on demand.

The behaviour being protected is that the deck keeps showing a sensible weather
tile: cached between refreshes rather than fetched per frame, and holding the
last good reading when the network is down instead of blanking the tile.
</remarks>
"""

from deckplate import weather
from deckplate.config import WeatherSettings


def test_describe_known_and_unknown_codes():
    """<summary>
    A forecast code becomes a label and an icon name, and an unrecognised code
    falls back rather than failing.
    </summary>
    <remarks>
    The code list is the upstream service's, not ours, and it gains values over
    time. Falling back to a cloud keeps an unknown code showing something on the
    deck instead of raising inside the refresh and losing the tile entirely.
    </remarks>
    """
    assert weather.describe(0) == ("Clear", "sun")
    assert weather.describe(63) == ("Rain", "rain")
    assert weather.describe(95) == ("Thunderstorm", "storm")
    assert weather.describe(1234) == ("Unknown", "cloud")


def test_parse_forecast_metric_and_imperial():
    """<summary>
    A forecast payload becomes an observation, with the temperature rounded and
    the unit following the configured system.
    </summary>
    <remarks>
    Rounding happens here rather than at drawing time because the tile is small and
    the rounded value is also what the signature is built from: leaving the
    fraction in would redraw the tile on changes too small to see.
    </remarks>
    """
    payload = {"current": {"temperature_2m": 12.6, "weather_code": 3}}
    obs = weather.parse_forecast(payload, "metric")
    assert obs.temperature_text == "13°C" and obs.label == "Overcast" and obs.icon == "cloud"
    assert weather.parse_forecast(payload, "imperial").unit == "°F"


class FakeClock:
    """
    <summary>
    A clock the test advances by hand.
    </summary>
    """
    def __init__(self):
        """
        <summary>
        Start at 1000 seconds.
        </summary>
        """
        self.t = 1000.0

    def __call__(self):
        """
        <summary>
        The current fake time.
        </summary>
        <returns>A float.</returns>
        """
        return self.t


def test_service_caches_and_refreshes_on_interval():
    """<summary>
    The service fetches once, serves the cached reading until the interval has
    passed, then fetches again and reports that the reading changed.
    </summary>
    <remarks>
    The return value is not "did it fetch", it is "did what is on screen need to
    change". That is what the run loop uses to decide whether to redraw, so a
    service that answered True on every call would push a tile to the hardware
    several times a second for no reason.
    </remarks>
    """
    clock = FakeClock()
    readings = [weather.Observation(10, 0, "Clear", "sun", "°C"),
                weather.Observation(11, 0, "Clear", "sun", "°C")]
    calls = []

    def fetcher(settings):
        """
        <summary>
        Record the fake time of the call; the first call returns the first reading, every later call the second.
        </summary>
        <param name="settings">Ignored.</param>
        <returns>An Observation.</returns>
        """
        calls.append(clock.t)
        return readings[min(len(calls) - 1, 1)]

    service = weather.WeatherService(WeatherSettings(refresh_minutes=10), fetcher=fetcher, monotonic=clock)
    assert service.refresh_if_due() is True
    assert service.observation.temperature == 10
    clock.t += 300
    assert service.refresh_if_due() is False  # not due yet
    clock.t += 300
    assert service.refresh_if_due() is True   # due, and the reading changed
    assert service.observation.temperature == 11
    assert len(calls) == 2


def test_service_keeps_last_reading_on_failure():
    """<summary>
    A failed fetch is logged, the last good reading is kept, and the retry comes
    sooner than the full interval.
    </summary>
    <remarks>
    An exception must never escape into the run loop here, because a brief loss of
    network would then take the whole daemon down. The tile keeps showing the last
    reading, which is a better answer than a blank one.

    The closing assertion is easy to misread. The retry succeeds, but the reading
    is identical to the one already on screen, so nothing changed and the answer is
    False: the service reports redraws needed, not fetches made.
    </remarks>
    """
    clock = FakeClock()
    state = {"fail": False}
    log = []

    def fetcher(settings):
        """
        <summary>
        Return light rain, or raise while the shared state says fail.
        </summary>
        <param name="settings">Ignored.</param>
        <returns>An Observation.</returns>
        <exception cref="ConnectionError">While state['fail'] is set.</exception>
        """
        if state["fail"]:
            raise ConnectionError("offline")
        return weather.Observation(9, 61, "Light rain", "rain", "°C")

    service = weather.WeatherService(WeatherSettings(refresh_minutes=10), fetcher=fetcher, monotonic=clock, log=log.append)
    assert service.refresh_if_due()
    state["fail"] = True
    clock.t += 601
    assert service.refresh_if_due() is False
    assert service.observation.temperature == 9
    assert log and "offline" in log[0]
    # retried sooner than the full interval after a failure
    clock.t += 121
    state["fail"] = False
    assert service.refresh_if_due() is False  # same reading, so not "changed"
