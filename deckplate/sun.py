"""<summary>
Sunrise and sunset for a place and a day, so the deck can dim itself at night.
</summary>
<remarks>
The standard NOAA solar calculation, done here in a few lines rather than
fetched, so the night setting works with no network and no account, from the
same coordinates the weather tile already uses. It is accurate to a minute or
two, which is more than a backlight needs.

Times come back in UTC, with the zone attached, and <see cref="is_night"/>
compares a zone aware now against them. A day on which the sun never rises or
never sets at those coordinates returns None for the pair, and that is
treated as day rather than night, so a deck in the far north is not dimmed
for a month.

Nothing here reads the clock: the moment is always an argument, which is
what makes it testable against known times.
</remarks>
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

ZENITH = 90.833  # the sun's centre 50 minutes of arc below the horizon: official sunrise


def sun_times(latitude: float, longitude: float, day: date) -> tuple[datetime, datetime] | None:
    """<summary>Sunrise and sunset on one day at one place.</summary>
    <param name="latitude">Degrees north.</param>
    <param name="longitude">Degrees east.</param>
    <param name="day">The calendar day, taken as the local day at those coordinates.</param>
    <returns>(sunrise, sunset) as UTC datetimes, or None when the sun does
    not rise or does not set that day.</returns>"""
    day_of_year = day.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (day_of_year - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    lat = math.radians(latitude)
    cos_ha = (math.cos(math.radians(ZENITH)) / (math.cos(lat) * math.cos(decl))
              - math.tan(lat) * math.tan(decl))
    if cos_ha < -1 or cos_ha > 1:
        return None
    ha = math.degrees(math.acos(cos_ha))
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    sunrise = midnight + timedelta(minutes=720 - 4 * (longitude + ha) - eqtime)
    sunset = midnight + timedelta(minutes=720 - 4 * (longitude - ha) - eqtime)
    return sunrise, sunset


def is_night(latitude: float, longitude: float, now: datetime) -> bool:
    """<summary>Whether it is between sunset and the next sunrise at a place.</summary>
    <param name="latitude">Degrees north.</param>
    <param name="longitude">Degrees east.</param>
    <param name="now">The moment, zone aware. A naive datetime is taken as local time.</param>
    <returns>True after sunset and before sunrise; False by day and on days
    without a sunrise or sunset.</returns>
    <remarks>Looks at yesterday, today and tomorrow in UTC, because near the
    date line a sunset belongs to a different UTC day than the local one and
    a single day's pair would miss it.</remarks>"""
    if now.tzinfo is None:
        now = now.astimezone()
    moment = now.astimezone(timezone.utc)
    today = moment.date()
    pairs = [sun_times(latitude, longitude, today + timedelta(days=offset)) for offset in (-1, 0, 1)]
    if all(pair is None for pair in pairs):
        return False
    return not any(pair is not None and pair[0] <= moment < pair[1] for pair in pairs)
