"""<summary>
The machine reading for the system panel, read from files in a temporary
folder shaped like /proc and /sys.
</summary>
"""

from deckplate import system


def test_cpu_times_and_memory_percent_parse_the_kernel_files():
    """<summary>The /proc parsers pick the right columns and round sensibly.</summary>"""
    idle, total = system.cpu_times("cpu  100 0 50 800 50 0 0 0 0 0\nother lines\n")
    assert (idle, total) == (850, 1000)
    assert system.memory_percent("MemTotal:       1000 kB\nMemFree:  10 kB\nMemAvailable:    250 kB\n") == 75
    assert system.memory_percent("MemTotal: 1000 kB\n") is None


def test_temperature_prefers_a_processor_sensor_then_a_thermal_zone(tmp_path):
    """<summary>
    A k10temp or coretemp sensor wins over other hwmon entries, and a thermal
    zone is the fallback when there is none.
    </summary>
    """
    hwmon = tmp_path / "hwmon"
    for index, (name, temp) in enumerate((("nvme", 61000), ("k10temp", 47500))):
        folder = hwmon / f"hwmon{index}"
        folder.mkdir(parents=True)
        (folder / "name").write_text(name + "\n")
        (folder / "temp1_input").write_text(f"{temp}\n")
    thermal = tmp_path / "thermal"
    zone = thermal / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "temp").write_text("39000\n")
    assert system.read_temperature(hwmon, thermal) == 48
    (hwmon / "hwmon1" / "name").write_text("sata\n")
    assert system.read_temperature(hwmon, thermal) == 39
    assert system.read_temperature(tmp_path / "none", tmp_path / "none2") is None


def test_service_needs_two_reads_for_the_processor_and_reports_changes(tmp_path):
    """<summary>
    The first reading has no processor figure, the second does, and a
    reading that did not move is not reported as a change.
    </summary>
    """
    stat = tmp_path / "stat"
    meminfo = tmp_path / "meminfo"
    stat.write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
    clock = {"t": 0.0}
    service = system.SystemService(monotonic=lambda: clock["t"], interval=2, stat=stat, meminfo=meminfo,
                                   hwmon=tmp_path / "nohw", thermal=tmp_path / "noth")
    assert service.refresh_if_due() is True
    assert service.reading == system.SystemReading(None, 50, None)
    clock["t"] = 2
    stat.write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")  # 200 more jiffies, 100 of them idle
    assert service.refresh_if_due() is True
    assert service.reading.cpu_percent == 50
    clock["t"] = 4
    stat.write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")
    assert service.refresh_if_due() is True  # cpu drops to 0 over a 0 span? no: span 0 keeps None
    assert service.reading.cpu_percent is None
