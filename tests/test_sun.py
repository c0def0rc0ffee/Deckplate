"""<summary>
Sunrise and sunset against known times, and the night test around them.
</summary>
<remarks>
The reference times are from the published almanac values for London on
the summer solstice and for a Channel Islands coordinate in November, to
the few minutes the NOAA method is good for. A deck's backlight does not
need better.
</remarks>
"""

from datetime import date, datetime, timedelta, timezone

from deckplate import sun


def test_london_midsummer_sunrise_and_sunset_are_within_a_few_minutes():
    """<summary>London, 21 June: sunrise about 03:43 UTC, sunset about 20:21 UTC.</summary>"""
    sunrise, sunset = sun.sun_times(51.5072, -0.1276, date(2026, 6, 21))
    assert abs((sunrise - datetime(2026, 6, 21, 3, 43, tzinfo=timezone.utc)).total_seconds()) < 300
    assert abs((sunset - datetime(2026, 6, 21, 20, 21, tzinfo=timezone.utc)).total_seconds()) < 300


def test_night_is_after_sunset_and_before_sunrise():
    """<summary>
    Noon is day, late evening is night, and the moment just after sunrise is
    day again; a naive datetime is taken as local time.
    </summary>
    """
    lat, lon = 49.45, -2.58
    sunrise, sunset = sun.sun_times(lat, lon, date(2026, 11, 1))
    assert not sun.is_night(lat, lon, datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc))
    assert sun.is_night(lat, lon, datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc))
    assert sun.is_night(lat, lon, sunset + timedelta(minutes=1))
    assert not sun.is_night(lat, lon, sunrise + timedelta(minutes=1))
    assert sun.is_night(lat, lon, datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc))
    local_noon = datetime(2026, 11, 1, 12, 0)
    assert not sun.is_night(lat, lon, local_noon)


def test_polar_day_and_night_count_as_day():
    """<summary>Where the sun does not rise or set, the deck is never dimmed.</summary>"""
    assert sun.sun_times(80.0, 0.0, date(2026, 6, 21)) is None
    assert sun.sun_times(80.0, 0.0, date(2026, 12, 21)) is None
    assert not sun.is_night(80.0, 0.0, datetime(2026, 12, 21, 12, 0, tzinfo=timezone.utc))
