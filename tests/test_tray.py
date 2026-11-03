"""<summary>
The tray icon, without a tray: pystray is replaced by a fake module.
</summary>
<remarks>
pystray needs a display and a tray to put the icon in, and the suite runs
where there is neither, so a stand in module is put in sys.modules before
the tray asks for it. That exercises everything of ours, the menu, the two
actions, following the config and the stop, and nothing of pystray's. The
icon picture is checked against the generator in tools, which is the one
place the two drawings are held to be the same.
</remarks>
"""

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

from deckplate import config as cfg
from deckplate import tray

ROOT = Path(__file__).resolve().parents[1]


class FakeIcon:
    """<summary>Stands in for pystray.Icon: records what it was given and what was done to it.</summary>"""

    detached = True
    instances: list = []

    def __init__(self, name, image, title, menu):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.visible = True
        self.running = False
        self.stopped = False
        FakeIcon.instances.append(self)

    def run_detached(self):
        if not FakeIcon.detached:
            raise NotImplementedError
        self.running = True

    def run(self):
        self.running = True

    def stop(self):
        self.stopped = True


class FakeItem:
    """<summary>Stands in for pystray.MenuItem.</summary>"""

    def __init__(self, text, action, default=False):
        self.text, self.action, self.default = text, action, default


@pytest.fixture
def fake_pystray(monkeypatch):
    """<summary>A pystray module made of the two fakes above, installed for one test.</summary>"""
    module = types.ModuleType("pystray")
    module.Icon = FakeIcon
    module.MenuItem = FakeItem
    module.Menu = lambda *items: list(items)
    FakeIcon.instances = []
    FakeIcon.detached = True
    monkeypatch.setitem(sys.modules, "pystray", module)
    return module


def test_the_icon_carries_open_and_quit_and_runs_them(fake_pystray):
    """<summary>Starting shows one icon whose two items call what the daemon handed in.</summary>
    <remarks>Open is the default item, so a click on the icon itself opens the
    window where the platform has such a thing.</remarks>"""
    opened, quit_calls, log = [], [], []
    icon = tray.Tray(open_page=lambda: opened.append(1), quit=lambda: quit_calls.append(1), log=log.append)
    assert not icon.running
    assert icon.start() is True
    assert icon.running and icon.start() is True  # a second start adds nothing
    assert len(FakeIcon.instances) == 1
    shown = FakeIcon.instances[0]
    assert shown.title == "Deckplate" and shown.running and shown.image.size == (tray.ICON_SIZE, tray.ICON_SIZE)
    assert [item.text for item in shown.menu] == [tray.OPEN_TEXT, tray.QUIT_TEXT]
    assert shown.menu[0].default and not shown.menu[1].default
    shown.menu[0].action(shown, shown.menu[0])
    shown.menu[1].action()
    assert opened == [1] and quit_calls == [1]
    assert "tray icon shown" in log


def test_a_backend_without_a_detached_run_gets_a_thread(fake_pystray):
    """<summary>NotImplementedError from run_detached falls back to running the loop on a thread.</summary>"""
    FakeIcon.detached = False
    icon = tray.Tray(open_page=lambda: None, quit=lambda: None, log=lambda t: None)
    assert icon.start() is True
    shown = FakeIcon.instances[0]
    for _ in range(50):
        if shown.running:
            break
        import time
        time.sleep(0.01)
    assert shown.running


def test_stop_hides_then_stops_and_is_safe_twice(fake_pystray):
    """<summary>The icon is hidden before it is stopped, and a second stop does nothing.</summary>"""
    log = []
    icon = tray.Tray(open_page=lambda: None, quit=lambda: None, log=log.append)
    icon.start()
    shown = FakeIcon.instances[0]
    icon.stop()
    assert shown.visible is False and shown.stopped and not icon.running
    icon.stop()
    assert log.count("tray icon removed") == 1


def test_follow_starts_and_stops_with_the_config(fake_pystray):
    """<summary>The reload hook shows the icon when [tray] wants it and removes it when not.</summary>"""
    icon = tray.Tray(open_page=lambda: None, quit=lambda: None, log=lambda t: None)
    on = cfg.parse('[[pages]]\nname = "A"', Path("/base"))
    off = cfg.parse('[tray]\nenabled = false\n[[pages]]\nname = "A"', Path("/base"))
    icon.follow(off)
    assert not icon.running and FakeIcon.instances == []
    icon.follow(on)
    assert icon.running
    icon.follow(on)
    assert len(FakeIcon.instances) == 1
    icon.follow(off)
    assert not icon.running and FakeIcon.instances[0].stopped


def test_no_pystray_is_one_log_line_and_no_icon(monkeypatch):
    """<summary>A machine without pystray gets one notice, not one per reload, and the daemon carries on.</summary>"""
    monkeypatch.setitem(sys.modules, "pystray", None)  # makes the import fail
    log = []
    icon = tray.Tray(open_page=lambda: None, quit=lambda: None, log=log.append)
    assert icon.start() is False
    assert icon.start() is False
    assert not icon.running
    assert len([line for line in log if line.startswith("no tray icon")]) == 1


def test_the_window_is_a_separate_gui_process():
    """<summary>Open runs the gui command detached, with no streams inherited, and says so.</summary>"""
    calls, log = [], []

    def popen(command, **options):
        calls.append((command, options))
        return object()

    assert tray.open_page(popen=popen, log=log.append) is True
    command, options = calls[0]
    assert command == tray.gui_command()
    assert command[-1] == "gui"
    assert options["stdin"] is subprocess.DEVNULL and options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL
    if sys.platform == "win32":
        assert "creationflags" in options
    else:
        assert options["start_new_session"] is True
    assert "configuration window opened from the tray" in log


def test_a_window_that_cannot_start_is_logged_not_raised():
    """<summary>An OSError from the launch becomes a log line and False.</summary>"""
    log = []

    def popen(command, **options):
        raise OSError("no such program")

    assert tray.open_page(popen=popen, log=log.append) is False
    assert any("could not open the configuration window" in line for line in log)


def test_gui_command_from_source_and_from_the_built_program(monkeypatch):
    """<summary>From source the window is python -m deckplate gui; built, it is the program itself.</summary>"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert tray.gui_command() == [sys.executable, "-m", "deckplate", "gui"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert tray.gui_command() == [sys.executable, "gui"]


def test_the_tray_icon_is_the_same_picture_as_the_generator_draws():
    """<summary>The in memory icon matches tools/make_icon.py pixel for pixel at 64.</summary>
    <remarks>The geometry is written in both places, because the built
    program does not carry the tools folder. This is what stops the two
    drifting: change one and this fails until the other follows.</remarks>"""
    spec = importlib.util.spec_from_file_location("make_icon", ROOT / "tools" / "make_icon.py")
    make_icon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_icon)
    theirs = make_icon.draw(64)
    ours = tray.draw_icon(64)
    assert ours.size == theirs.size == (64, 64)
    assert ours.tobytes() == theirs.tobytes()
