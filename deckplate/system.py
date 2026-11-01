"""<summary>
How busy and how warm the machine is, for the system panel on the strip.
</summary>
<remarks>
Read straight from the kernel's files on Linux: the processor from the
first line of /proc/stat as a difference between two reads, memory from
/proc/meminfo, and the temperature from the first hwmon sensor whose name
looks like a processor sensor (k10temp, coretemp, zenpower), else the first
thermal zone. Nothing is installed and nothing is run.

Elsewhere, or where a file is missing, the reading carries None for that
value and the panel draws a dash, so the tile never breaks a deck on a
machine that cannot answer.

Shaped like the weather service so the controller drives it the same way.
</remarks>
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REFRESH_SECONDS = 2.0
PROC_STAT = Path("/proc/stat")
PROC_MEMINFO = Path("/proc/meminfo")
HWMON = Path("/sys/class/hwmon")
THERMAL = Path("/sys/class/thermal")
CPU_SENSORS = ("k10temp", "coretemp", "zenpower", "cpu_thermal", "x86_pkg_temp")


@dataclass(frozen=True)
class SystemReading:
    """<summary>One reading: processor load, memory in use and temperature.</summary>
    <param name="cpu_percent">0 to 100 across every core, or None when unknown.</param>
    <param name="memory_percent">0 to 100 of memory in use, or None.</param>
    <param name="temperature_c">The processor temperature in Celsius, or None.</param>"""

    cpu_percent: int | None
    memory_percent: int | None
    temperature_c: int | None


def cpu_times(text: str) -> tuple[int, int]:
    """<summary>Idle and total jiffies from the first line of /proc/stat.</summary>
    <param name="text">The file's text, or at least its first line.</param>
    <returns>(idle, total).</returns>
    <exception cref="ValueError">The line is not a cpu line.</exception>"""
    fields = text.splitlines()[0].split()
    if not fields or fields[0] != "cpu":
        raise ValueError("not a /proc/stat cpu line")
    numbers = [int(value) for value in fields[1:]]
    idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
    return idle, sum(numbers)


def memory_percent(text: str) -> int | None:
    """<summary>Memory in use from /proc/meminfo text, as a whole percent.</summary>
    <param name="text">The file's text.</param>
    <returns>0 to 100, or None when the two fields are missing.</returns>"""
    values = {}
    for line in text.splitlines():
        name, _sep, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[name] = int(parts[0])
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None
    return max(0, min(100, round((total - available) * 100 / total)))


def read_temperature(hwmon: Path = HWMON, thermal: Path = THERMAL) -> int | None:
    """<summary>The processor temperature in whole Celsius, or None.</summary>
    <param name="hwmon">The hwmon class folder.</param>
    <param name="thermal">The thermal class folder, used when no sensor matches.</param>
    <returns>Degrees, or None when nothing readable is found.</returns>"""
    try:
        for sensor in sorted(hwmon.glob("hwmon*")):
            try:
                name = (sensor / "name").read_text().strip()
            except OSError:
                continue
            if name in CPU_SENSORS:
                for probe in sorted(sensor.glob("temp*_input")):
                    try:
                        return round(int(probe.read_text().strip()) / 1000)
                    except (OSError, ValueError):
                        continue
        for zone in sorted(thermal.glob("thermal_zone*")):
            try:
                return round(int((zone / "temp").read_text().strip()) / 1000)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return None


class SystemService:
    """<summary>Reads the machine on a short timer and keeps the latest reading.</summary>
    <remarks>The processor figure needs two reads, so the first reading
    after start has None there and the panel shows a dash for a couple of
    seconds. Every file read is injectable through the paths, and the tests
    point them at a temporary folder.</remarks>"""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic,
                 interval: float = REFRESH_SECONDS, stat: Path = PROC_STAT,
                 meminfo: Path = PROC_MEMINFO, hwmon: Path = HWMON, thermal: Path = THERMAL) -> None:
        """<summary>Build the service. Nothing is read here.</summary>
        <param name="monotonic">The clock.</param>
        <param name="interval">Seconds between reads.</param>
        <param name="stat">Where /proc/stat is.</param>
        <param name="meminfo">Where /proc/meminfo is.</param>
        <param name="hwmon">The hwmon folder.</param>
        <param name="thermal">The thermal folder.</param>"""
        self.monotonic = monotonic
        self.interval = interval
        self.stat = stat
        self.meminfo = meminfo
        self.hwmon = hwmon
        self.thermal = thermal
        self.reading: SystemReading | None = None
        self._next_due = 0.0
        self._last: tuple[int, int] | None = None

    def read(self) -> SystemReading:
        """<summary>Read everything once.</summary>
        <returns>The reading, with None for whatever could not be read.</returns>"""
        cpu = None
        try:
            idle, total = cpu_times(self.stat.read_text())
            if self._last is not None:
                last_idle, last_total = self._last
                span = total - last_total
                if span > 0:
                    cpu = max(0, min(100, round((span - (idle - last_idle)) * 100 / span)))
            self._last = (idle, total)
        except (OSError, ValueError, IndexError):
            self._last = None
        try:
            memory = memory_percent(self.meminfo.read_text())
        except OSError:
            memory = None
        return SystemReading(cpu_percent=cpu, memory_percent=memory,
                             temperature_c=read_temperature(self.hwmon, self.thermal))

    def refresh_if_due(self) -> bool:
        """<summary>Read again when the interval has passed.</summary>
        <returns>True when the reading changed.</returns>"""
        if self.monotonic() < self._next_due:
            return False
        self._next_due = self.monotonic() + self.interval
        fresh = self.read()
        changed = fresh != self.reading
        self.reading = fresh
        return changed
