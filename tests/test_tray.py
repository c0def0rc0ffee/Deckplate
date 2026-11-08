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
import threading
import time
import types
from pathlib import Path

import pytest

from deckplate import config as cfg
from deckplate import tray

ROOT = Path(__file__).resolve().parents[1]


class FakeIcon:
    """<summary>Stands in for pystray.Icon: records what it was given and what was done to it.</summary>
    <remarks>Starts hidden and runs the setup hook on a thread of its own, as
    pystray does, so the first showing is the Tray's own doing. With
    hold_setup the hook is kept for the test to run when it chooses.</remarks>"""

    fail_run = False
    hold_setup = False
    instances: list = []

    def __init__(self, name, image, title, menu):
        """<summary>Take what pystray.Icon takes and start hidden and not running.</summary>"""
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.visible = False
        self.running = False
        self.detached = False
        self.stopped = False
        self.setup = None
        FakeIcon.instances.append(self)

    def _begin(self, setup):
        """<summary>Mark the loop as running and hand the setup hook to a thread, unless held.</summary>"""
        self.running = True
        self.setup = setup
        if setup is not None and not FakeIcon.hold_setup:
            threading.Thread(target=setup, args=(self,), daemon=True).start()

    def run_detached(self, setup=None):
        """<summary>The macOS path: recorded as detached, then begun.</summary>"""
        self.detached = True
        self._begin(setup)

    def run(self, setup=None):
        """<summary>The thread path: begun, or a failure when fail_run is set.</summary>"""
        if FakeIcon.fail_run:
            raise RuntimeError("no tray to sit in")
        self._begin(setup)

    def stop(self):
        """<summary>Record that the loop was ended.</summary>"""
        self.stopped = True


class FakeItem:
    """<summary>Stands in for pystray.MenuItem.</summary>"""

    def __init__(self, text, action, default=False):
        """<summary>Keep the label, the action and whether it is the default item.</summary>"""
        self.text, self.action, self.default = text, action, default


def wait_for(condition) -> bool:
    """<summary>Poll a condition for up to half a second, for work done on the tray thread.</summary>"""
    for _ in range(50):
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def fake_pystray(monkeypatch):
    """<summary>A pystray module made of the two fakes above, installed for one test.</summary>"""
    module = types.ModuleType("pystray")
    module.Icon = FakeIcon
    module.MenuItem = FakeItem
    module.Menu = lambda *items: list(items)
    FakeIcon.instances = []
    FakeIcon.fail_run = False
    FakeIcon.hold_setup = False
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
    assert wait_for(lambda: shown.running and shown.visible)
    assert shown.title == "Deckplate" and shown.image.size == (tray.ICON_SIZE, tray.ICON_SIZE)
    assert [item.text for item in shown.menu] == [tray.OPEN_TEXT, tray.QUIT_TEXT]
    assert shown.menu[0].default and not shown.menu[1].default
    shown.menu[0].action(shown, shown.menu[0])
    shown.menu[1].action()
    assert opened == [1] and quit_calls == [1]
    assert "tray icon shown" in log


def test_the_loop_runs_on_its_own_thread_not_detached(fake_pystray, monkeypatch):
    """<summary>Off macOS the icon's loop is run on the tray thread, never detached.</summary>
    <remarks>The regression: on Linux run_detached left the AppIndicator
    waiting for a GLib loop the daemon never runs, so the log said the icon
    was shown while KDE never saw it. run brings the loop with it.</remarks>"""
    monkeypatch.setattr(sys, "platform", "linux")
    icon = tray.Tray(open_page=lambda: None, quit=lambda: None, log=lambda t: None)
    assert icon.start() is True
    shown = FakeIcon.instances[0]
    assert wait_for(lambda: shown.running)
    assert not shown.detached


def test_macos_keeps_the_detached_run(fake_pystray, monkeypatch):
    """<summary>On macOS the loop must own the main thread, so the icon is run detached.</summary>"""
    monkeypatch.setattr(sys, "platform", "darwin")
    icon = tray.Tray(open_page=lambda: None, quit=lambda: None, log=lambda t: None)
    assert icon.start() is True
    shown = FakeIcon.instances[0]
    assert shown.detached
    assert wait_for(lambda: shown.visible)


def test_a_loop_that_fails_is_logged_and_forgotten(fake_pystray, monkeypatch):
    """<summary>A tray loop that raises is one log line, and the next follow may try again.</summary>"""
    monkeypatch.setattr(sys, "platform", "linux")
    FakeIcon.fail_run = True
    log = []
    icon = tray.Tray(open_page=lambda: None, quit=lambda: None, log=log.append)
    assert icon.start() is True
    assert wait_for(lambda: not icon.running)
    assert any(line.startswith("tray icon stopped unexpectedly") for line in log)
    FakeIcon.fail_run = False
    assert icon.start() is True and len(FakeIcon.instances) == 2


def test_stop_hides_then_stops_and_is_safe_twice(fake_pystray):
    """<summary>The icon is hidden before it is stopped, and a second stop does nothing.</summary>"""
    log = []
    icon = tray.Tray(open_page=lambda: None, quit=lambda: None, log=log.append)
    icon.start()
    shown = FakeIcon.instances[0]
    assert wait_for(lambda: shown.visible)
    icon.stop()
    assert shown.visible is False and shown.stopped and not icon.running
    icon.stop()
    assert log.count("tray icon removed") == 1


def test_follow_hides_and_shows_one_icon_without_stopping_it(fake_pystray):
    """<summary>The reload hook shows the icon when [tray] wants it and hides it when not, never ending its loop.</summary>
    <remarks>An icon whose loop is stopped stays registered with a Linux
    desktop until the process ends, frozen, so the tick only hides it.</remarks>"""
    log = []
    icon = tray.Tray(open_page=lambda: None, quit=lambda: None, log=log.append)
    on = cfg.parse('[[pages]]\nname = "A"', Path("/base"))
    off = cfg.parse('[tray]\nenabled = false\n[[pages]]\nname = "A"', Path("/base"))
    icon.follow(off)
    assert not icon.running and FakeIcon.instances == []
    icon.follow(on)
    shown = FakeIcon.instances[0]
    assert icon.running and wait_for(lambda: shown.visible)
    icon.follow(on)
    icon.follow(off)
    assert not icon.running and shown.visible is False and not shown.stopped
    icon.follow(off)
    assert log.count("tray icon hidden") == 1
    icon.follow(on)
    assert icon.running and shown.visible and len(FakeIcon.instances) == 1


def test_a_hide_before_the_loop_is_up_is_not_undone(fake_pystray):
    """<summary>The setup hook shows the icon only if it is still wanted when the loop starts.</summary>"""
    FakeIcon.hold_setup = True
    icon = tray.Tray(open_page=lambda: None, quit=lambda: None, log=lambda t: None)
    icon.start()
    shown = FakeIcon.instances[0]
    assert wait_for(lambda: shown.setup is not None)
    icon.hide()
    shown.setup(shown)
    assert shown.visible is False and not icon.running


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
        """<summary>Record the launch instead of starting anything.</summary>"""
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
        """<summary>Fail the launch as a missing program would.</summary>"""
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
