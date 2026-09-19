"""<summary>
Current weather from Open Meteo, and the small service that keeps one reading
fresh for the weather tile.
</summary>
<remarks>
Open Meteo is free and needs no account, so there is no API key anywhere in
this project and none should ever be added here: a key would be a credential
in a config file for no gain. The two endpoints used are the forecast one and
the geocoder that turns a typed place name into coordinates for the
configuration page.

One request every refresh interval, no more. If a request fails the last good
reading is kept and shown, so a flaky connection never blanks the tile, and
the retry comes sooner than the normal interval rather than waiting the whole
period out.

Nothing here draws. The tiles module turns an <see cref="Observation"/> into a
picture, which is what lets the whole of this file be tested with a fake
fetcher and no network at all.
</remarks>
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import requests

from .config import WeatherSettings

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
USER_AGENT = "deckplate"

# WMO weather interpretation codes as used by Open Meteo: (short label, icon).
# The labels are deliberately short because they are drawn along the bottom of
# a key a few dozen pixels wide, and the icon names are the ones
# tiles.draw_icon knows how to draw. Several codes share a label on purpose:
# the difference between drizzle at 51 and at 55 does not survive being shown
# that small.
WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("Clear", "sun"),
    1: ("Mostly clear", "sun"),
    2: ("Partly cloudy", "partly"),
    3: ("Overcast", "cloud"),
    45: ("Fog", "fog"),
    48: ("Rime fog", "fog"),
    51: ("Drizzle", "rain"),
    53: ("Drizzle", "rain"),
    55: ("Drizzle", "rain"),
    56: ("Freezing drizzle", "rain"),
    57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"),
    67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Showers", "rain"),
    81: ("Showers", "rain"),
    82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"),
    86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"),
    96: ("Thunderstorm", "storm"),
    99: ("Thunderstorm", "storm"),
}


def describe(code: int) -> tuple[str, str]:
    """<summary>
    The short label and icon name for a WMO weather code.
    </summary>
    <param name="code">A WMO interpretation code as Open Meteo reports it.</param>
    <returns>A (label, icon) pair.</returns>
    <remarks>
    An unknown code gives "Unknown" and a plain cloud rather than raising,
    because the table is a snapshot of a list that can gain entries: a code
    nobody has seen yet should draw something sensible on the key instead of
    stopping the refresh. A tile reading "Unknown" is the sign a code is
    missing from <see cref="WMO_CODES"/>.
    </remarks>
    """
    return WMO_CODES.get(code, ("Unknown", "cloud"))


@dataclass(frozen=True)
class Observation:
    """<summary>
    One weather reading, already decoded and ready to draw.
    </summary>
    <remarks>
    ``temperature`` is in whatever unit was asked for, and ``unit`` carries
    the degree symbol to print with it, so nothing downstream has to know
    which setting was used. ``label`` and ``icon`` are the decoded form of
    ``code`` and are stored rather than looked up again, which keeps the
    drawing code free of the code table.
    Frozen and compared by value on purpose: <see cref="WeatherService"/>
    uses equality to decide whether the tile actually needs redrawing, and a
    mutable reading would defeat that.
    </remarks>"""

    temperature: float
    code: int
    label: str
    icon: str
    unit: str

    @property
    def temperature_text(self) -> str:
        """<summary>
        The temperature as it goes on the key: rounded to a whole degree,
        with the unit.
        </summary>
        <returns>A string such as "12°C".</returns>
        <remarks>
        Whole degrees because a key is too small for a decimal place to be
        read at arm's length. Rounding here rather than at the fetch keeps
        the stored reading exact, so a change of half a degree still counts
        as a change even when the printed text does not move.
        </remarks>
        """
        return f"{round(self.temperature)}{self.unit}"


@dataclass(frozen=True)
class Place:
    """<summary>
    One result from the geocoder: a place name with the coordinates the
    forecast endpoint needs.
    </summary>
    <remarks>
    ``region`` is the geocoder's first level administrative area and is
    often empty, as is ``country`` for some results, so both are stored as
    empty strings rather than None and can be shown without a check. The
    configuration page offers these as a list and writes the chosen
    latitude and longitude into the settings, which is the only part the
    daemon ever sees.
    </remarks>"""

    name: str
    region: str
    country: str
    latitude: float
    longitude: float


def fetch(settings: WeatherSettings, timeout: float = 10) -> Observation:
    """<summary>
    Ask Open Meteo for the current conditions at the configured coordinates.
    </summary>
    <param name="settings">Coordinates and the unit preference.</param>
    <param name="timeout">Seconds to wait for the reply.</param>
    <returns>The decoded reading.</returns>
    <remarks>
    Blocking, and called from the controller's own loop, which is why the
    timeout is short and why <see cref="WeatherService.refresh_if_due"/>
    catches everything around it: a slow reply must never hold up key
    presses for longer than this.
    No account and no key: the only thing identifying the caller is a plain
    user agent string. Anything sent here is the latitude and longitude from
    the config and nothing else.
    "timezone=auto" is asked for so the server resolves the local time at
    those coordinates, which matters for the daily fields even though the
    current reading is the only one read back.
    </remarks>
    
    <exception cref="requests.RequestException">The request failed, timed out
    or came back as an error status.</exception>"""
    params = {
        "latitude": settings.latitude,
        "longitude": settings.longitude,
        "current": "temperature_2m,weather_code",
        "temperature_unit": "fahrenheit" if settings.units == "imperial" else "celsius",
        "timezone": "auto",
    }
    response = requests.get(FORECAST_URL, params=params, timeout=timeout,
                            headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return parse_forecast(response.json(), settings.units)


def parse_forecast(payload: dict, units: str) -> Observation:
    """<summary>
    Turn a decoded forecast reply into an <see cref="Observation"/>.
    </summary>
    <param name="payload">The reply body, already parsed from JSON.</param>
    <param name="units">"imperial" for Fahrenheit, anything else for
    Celsius. It must match what was asked for in the request, because the
    reply carries the numbers and this decides what to label them.</param>
    <returns>The reading.</returns>
    <remarks>
    Split out from <see cref="fetch"/> so the decode can be tested against
    saved replies with no network. It is strict on purpose: a missing field
    raises here rather than producing a half filled reading that would be
    drawn as though it were real, and the caller keeps the previous reading
    when it does.
    </remarks>
    
    <exception cref="KeyError">The reply has no "current" block or is
    missing a field, which is what a changed API looks like.</exception>"""
    current = payload["current"]
    code = int(current["weather_code"])
    label, icon = describe(code)
    return Observation(
        temperature=float(current["temperature_2m"]),
        code=code, label=label, icon=icon,
        unit="°F" if units == "imperial" else "°C",
    )


def geocode(name: str, count: int = 5, timeout: float = 10) -> list[Place]:
    """<summary>
    Look a place name up and return the candidates to choose between.
    </summary>
    <param name="name">What the user typed, such as a town or a postcode.</param>
    <param name="count">How many candidates to ask for.</param>
    <param name="timeout">Seconds to wait for the reply.</param>
    <returns>The matches in the geocoder's own order, best first. An empty
    list is the normal answer for a name nothing matches, not an error.</returns>
    <remarks>
    Used by the configuration page only. The daemon never calls this: it
    works from the coordinates already written into the settings, so a
    running deck needs no geocoder and keeps working if that endpoint is
    down.
    Results are asked for in English so the labels match the rest of the
    page whatever the machine's locale says.
    </remarks>
    
    <exception cref="requests.RequestException">The request failed, timed out
    or came back as an error status.</exception>"""
    response = requests.get(GEOCODE_URL, params={"name": name, "count": count, "language": "en"},
                            timeout=timeout, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    places = []
    for item in response.json().get("results", []):
        places.append(Place(
            name=item.get("name", ""),
            region=item.get("admin1", ""),
            country=item.get("country", ""),
            latitude=float(item["latitude"]),
            longitude=float(item["longitude"]),
        ))
    return places


class WeatherService:
    """<summary>
    Holds the latest reading and refreshes it on a timer.
    </summary>
    <remarks>
    Driven by being called, not by a thread of its own: the controller calls
    <see cref="refresh_if_due"/> on each pass of its loop and the timer
    inside decides whether anything actually happens. That keeps every
    network call on one known thread and keeps the class testable with a
    fake clock.
    The reading is public and starts as None, which the tiles module draws as
    "no weather" rather than treating as an error, so the deck comes up
    looking right before the first reply arrives.
    </remarks>
    """

    def __init__(self, settings: WeatherSettings, *,
                 fetcher: Callable[[WeatherSettings], Observation] = fetch,
                 monotonic: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print) -> None:
        """<summary>
        Build the service. Nothing is fetched here, so this is safe to
        construct with no network.
        </summary>
        <param name="settings">Coordinates, units and the refresh interval.
        Held by reference, so a settings object replaced on reload must be
        given to a new service.</param>
        <param name="fetcher">How to get a reading. Swapped in the tests for
        one that returns canned readings or raises on demand.</param>
        <param name="monotonic">The clock. Monotonic rather than wall clock
        on purpose: the interval must survive the system time being set
        backwards, which a daemon running for weeks will see.</param>
        <param name="log">Where failures go. One line per failed refresh,
        never the reply body.</param>
        <remarks>
        The due time starts at zero, so the first call to
        <see cref="refresh_if_due"/> always fetches.
        </remarks>
        """
        self.settings = settings
        self.fetcher = fetcher
        self.monotonic = monotonic
        self.log = log
        self.observation: Observation | None = None
        self._next_due = 0.0

    def refresh_if_due(self) -> bool:
        """<summary>
        Fetch a new reading when the interval has passed, and say whether the
        tile now needs redrawing.
        </summary>
        <returns>True when the reading actually changed, False when it is not
        yet due, when nothing moved, or when the fetch failed.</returns>
        <remarks>
        Cheap to call as often as the loop likes: everything but the clock
        read is skipped until the interval is up.
        Every failure is caught, including a bad reply as well as the network
        being down, because a weather tile must never be able to stop the
        deck. The previous reading stays on screen and the next attempt is
        brought forward to at most two minutes, so a short outage recovers
        quickly without hammering the endpoint when the interval is already
        shorter than that.
        False after a failure means the tile is not redrawn, which is what
        keeps the last good reading visible rather than blanking it.
        </remarks>
        """
        if self.monotonic() < self._next_due:
            return False
        interval = self.settings.refresh_minutes * 60
        try:
            fresh = self.fetcher(self.settings)
        except Exception as err:  # network down, bad reply: keep the old reading
            self.log(f"weather refresh failed: {err}")
            self._next_due = self.monotonic() + min(interval, 120)
            return False
        self._next_due = self.monotonic() + interval
        changed = fresh != self.observation
        self.observation = fresh
        return changed
