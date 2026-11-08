"""<summary>
The configuration window's single instance guard, without a window.
</summary>
<remarks>
pywebview is replaced by a fake module so nothing is drawn, and every lock
and port file lives in the test's own folder, so a window really open on the
machine running the suite is never met. What is covered: a second launch
opens nothing and asks the first forward, the listener ignores anything but
its own message, and a free lock opens the window with the listener wired to
its restore.
</remarks>
"""

import sys
import time
import types
from pathlib import Path

import pytest

from deckplate import gui, instance


def wait_for(condition) -> bool:
    """<summary>Poll a condition for up to a second, for work done on the listener's thread.</summary>"""
    for _ in range(100):
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class FakeWindow:
    """<summary>Stands in for a pywebview window: counts the restores asked of it.</summary>"""

    def __init__(self) -> None:
        """<summary>Start with no restores.</summary>"""
        self.restored = 0

    def restore(self) -> None:
        """<summary>Record one request to come forward.</summary>"""
        self.restored += 1


@pytest.fixture
def fake_webview(monkeypatch):
    """<summary>A pywebview module whose window is a FakeWindow and whose start returns at once.</summary>"""
    module = types.ModuleType("webview")
    module.windows = []
    module.started = 0

    def create_window(*_args, **_kwargs):
        """<summary>Make and keep a FakeWindow.</summary>"""
        window = FakeWindow()
        module.windows.append(window)
        return window

    def start(**_kwargs):
        """<summary>Count the start; the real one blocks until the window closes.</summary>"""
        module.started += 1

    module.create_window = create_window
    module.start = start
    monkeypatch.setitem(sys.modules, "webview", module)
    return module


def test_no_port_file_means_nothing_to_ask(tmp_path: Path):
    """<summary>A window still starting has the lock but no port yet: asking says False, no error.</summary>"""
    assert gui.ask_window_forward(tmp_path / "missing.port") is False


def test_the_listener_brings_the_window_forward(tmp_path: Path):
    """<summary>The port is published and a request through it calls the restore.</summary>"""
    calls = []
    port_file = tmp_path / "window.port"
    listener = gui.RaiseListener(port_file, lambda: calls.append(1), log=lambda t: None)
    assert listener.start() is True
    assert port_file.read_text().strip() == str(listener.port)
    assert gui.ask_window_forward(port_file) is True
    assert wait_for(lambda: calls == [1])


def test_the_listener_ignores_anything_else(tmp_path: Path):
    """<summary>A stranger on the port, or a stale port file, cannot make the window do anything.</summary>"""
    import socket

    calls = []
    port_file = tmp_path / "window.port"
    listener = gui.RaiseListener(port_file, lambda: calls.append(1), log=lambda t: None)
    listener.start()
    with socket.create_connection(("127.0.0.1", listener.port), timeout=2) as conn:
        conn.sendall(b"GET / HTTP/1.0\r\n\r\n")
    assert gui.ask_window_forward(port_file) is True
    assert wait_for(lambda: calls == [1])
    time.sleep(0.05)
    assert calls == [1]


def test_a_second_launch_opens_nothing_and_asks_the_first_forward(tmp_path: Path, fake_webview):
    """<summary>With the window lock held, open_window makes no window and sends the request.</summary>"""
    lock_file, port_file = tmp_path / "window.lock", tmp_path / "window.port"
    held = instance.claim(lock_file)
    first = []
    gui.RaiseListener(port_file, lambda: first.append(1), log=lambda t: None).start()
    log = []
    assert gui.open_window("http://127.0.0.1:1/", log=log.append, lock_file=lock_file, port_file=port_file) == 0
    assert fake_webview.windows == [] and fake_webview.started == 0
    assert wait_for(lambda: first == [1])
    assert any("already open" in line for line in log)
    held.close()


def test_a_free_lock_opens_the_window_with_its_listener(tmp_path: Path, fake_webview):
    """<summary>The first launch opens the window, publishes the port and wires a request to restore.</summary>"""
    lock_file, port_file = tmp_path / "window.lock", tmp_path / "window.port"
    assert gui.open_window("http://127.0.0.1:1/", log=lambda t: None, lock_file=lock_file, port_file=port_file) == 0
    assert fake_webview.started == 1 and len(fake_webview.windows) == 1
    assert gui.ask_window_forward(port_file) is True
    assert wait_for(lambda: fake_webview.windows[0].restored == 1)


def test_the_daemon_lock_keeps_its_name(monkeypatch, tmp_path: Path):
    """<summary>Naming the lock did not move the daemon's: the default is still deckplate.lock.</summary>"""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert instance.lock_path() == tmp_path / "deckplate.lock"
    assert instance.lock_path(gui.WINDOW_LOCK_NAME) == tmp_path / "deckplate-window.lock"
