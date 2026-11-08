"""<summary>
Open the configuration page in its own window.
</summary>
<remarks>
The page is served by the running daemon. This just puts a native window
around it with pywebview (WebView2 on Windows, WebKitGTK on Linux), and falls
back to the normal browser when pywebview is not installed.

Nothing here talks to the deck, or even knows there is one. It is a window
around a URL, so a fault in this module can slow the page down but can never
reach the hardware. The daemon has to already be running: this module starts
nothing, and says so plainly rather than trying to launch one, because two
daemons would fight over the same USB device.

pywebview is an optional dependency on purpose. It is imported inside the
function that needs it so that importing this module, which the command line
does for every ``gui`` run, costs nothing on a machine without it.

One window per user. The desktop launcher and the tray's Open both run
``deckplate gui`` on every click, so without a guard each click stacked up
another window. The first window takes a lock of its own beside the daemon's
(see <see cref="instance"/>) and listens on a loopback port; a later launch
finds the lock held, asks that window to come forward and exits without
opening one. Whether the desktop then raises it or only marks it in the task
bar is the window manager's call: KDE on Wayland may do either, depending on
its focus stealing setting.
</remarks>
"""

from __future__ import annotations

import http.client
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable

from . import instance

WINDOW_TITLE = "Deckplate"
WINDOW_SIZE = (1180, 780)
# The window and taskbar icon: the same logo the desktop entry and the page use.
# It sits in the page folder, which the built program carries beside this module.
WINDOW_ICON = Path(__file__).with_name("web") / "logo.svg"
# The window's own lock, and the file beside it that holds the port the open
# window listens on, both in the daemon lock's folder.
WINDOW_LOCK_NAME = "deckplate-window.lock"
WINDOW_PORT_NAME = "deckplate-window.port"
# What a later launch sends the open window. Anything else on that port is
# ignored, so a stale port file that now names some other program's port
# cannot make the window do anything.
RAISE_MESSAGE = b"deckplate: come forward\n"


def ask_window_forward(port_file: Path) -> bool:
    """<summary>
    Ask the window that is already open to come forward.
    </summary>
    <param name="port_file">The file the open window wrote its port into.</param>
    <returns>True when the request was delivered, False when there was no
    port to send it to or nothing took it.</returns>
    <remarks>
    False is not an error. The commonest cause is a window still starting
    up, which has the lock but has not written its port yet; it is about to
    appear anyway. Loopback only, two seconds at most, nothing read back.
    </remarks>
    """
    try:
        port = int(port_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
            conn.sendall(RAISE_MESSAGE)
    except OSError:
        return False
    return True


class RaiseListener:
    """<summary>
    The open window's ear: a loopback port that brings the window forward.
    </summary>
    <remarks>
    Bound to 127.0.0.1 on a port the system picks, which is written to the
    port file for a later launch to read. The file is written to a temporary
    name and renamed into place, so a reader never sees half a number. The
    accepting thread is a daemon thread and the socket goes with the process;
    there is nothing to tidy when the window closes.

    Each connection is read for at most a second and must carry exactly
    <see cref="RAISE_MESSAGE"/>; anything else is dropped unanswered.
    </remarks>
    """

    def __init__(self, port_file: Path, on_raise: Callable[[], object], log=print) -> None:
        """<summary>Build over the port file and what a request does, without listening yet.</summary>
        <param name="port_file">Where the port is written.</param>
        <param name="on_raise">Called on the listener's thread for each request.</param>
        <param name="log">Where a failure to listen is reported.</param>"""
        self.port_file = port_file
        self.on_raise = on_raise
        self.log = log
        self.port: int | None = None

    def start(self) -> bool:
        """<summary>Listen, publish the port and start the accepting thread.</summary>
        <returns>True when listening, False when the port or the file could not be had.</returns>
        <remarks>A False costs only the bring forward: a later launch then
        finds the lock held, cannot reach this window, and still opens no
        second one.</remarks>"""
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.bind(("127.0.0.1", 0))
            server.listen(4)
            self.port = server.getsockname()[1]
            partial = self.port_file.with_name(self.port_file.name + ".new")
            partial.write_text(f"{self.port}\n", encoding="ascii")
            os.replace(partial, self.port_file)
        except OSError as err:
            self.log(f"the window cannot be brought forward by a later launch ({err})")
            return False
        threading.Thread(target=self._serve, args=(server,), name="deckplate-window-raise",
                         daemon=True).start()
        return True

    def _serve(self, server: socket.socket) -> None:
        """<summary>Accept requests for the life of the process.</summary>
        <param name="server">The listening socket.</param>"""
        while True:
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(1)
                    received = b""
                    while len(received) < len(RAISE_MESSAGE):
                        chunk = conn.recv(len(RAISE_MESSAGE) - len(received))
                        if not chunk:
                            break
                        received += chunk
                except OSError:
                    continue
            if received == RAISE_MESSAGE:
                try:
                    self.on_raise()
                except Exception:  # a window mid close must not end the listener
                    pass


def daemon_reachable(host: str, port: int) -> bool:
    """<summary>
    Ask the daemon's API whether it is there, before a window is opened on it.
    </summary>
    <param name="host">Loopback name or address the daemon binds, such as
    127.0.0.1.</param>
    <param name="port">The API port from the config file.</param>
    <returns>True when something answered as the daemon, False otherwise.</returns>
    <remarks>
    503 counts as reachable as well as 200. That is what ``/api/state``
    answers when the daemon is up but no deck is plugged in, which is a
    perfectly good reason to open the page: the page itself explains the
    missing deck far better than this function could.

    Any OSError is treated as "not there" rather than raised, because a
    refused connection is the normal answer when the daemon is simply not
    running. The two second timeout is a deliberate ceiling: a wait longer
    than that looks like a hung launcher to whoever clicked the icon.
    </remarks>
    """
    try:
        conn = http.client.HTTPConnection(host, port, timeout=2)
        conn.request("GET", "/api/state")
        status = conn.getresponse().status
        conn.close()
        return status in (200, 503)
    except OSError:
        return False


def open_window(url: str, log=print, lock_file: Path | None = None, port_file: Path | None = None) -> int:
    """<summary>
    Put a native window around the page, or fall back to the browser.
    </summary>
    <param name="url">The daemon's page, already checked as reachable.</param>
    <param name="log">Where the fallback notices go; the command line passes
    its own timestamped printer.</param>
    <param name="lock_file">The window lock, or None for the per user one.</param>
    <param name="port_file">The open window's port file, or None for the per user one.</param>
    <returns>Always 0. A launch that finds a window already open asks it
    forward and returns 0 too. Falling back to the browser is a normal outcome, not a
    failure, so there is no exit code that means "no window".</returns>
    <remarks>
    This blocks until the window is closed: ``webview.start()`` runs the
    native event loop on the calling thread and only returns at the end. It
    must therefore be called from the main thread, which is where the command
    line calls it from.

    Two separate fallbacks, for two separate faults. pywebview missing is an
    ImportError and means the optional dependency was never installed. A
    broad Exception around ``start`` catches the other case: pywebview is
    present but the machine has no web engine behind it, which surfaces as
    assorted platform specific errors rather than one type worth naming.

    The icon is only passed when the file is there, so a page folder missing
    its logo costs the window its icon and nothing else. pywebview uses it on
    Linux; on Windows the icon comes from the exe and this is ignored.

    The window lock is taken only once pywebview is known to be there, so
    the browser fallback, which opens a tab rather than a window, is never
    refused. It is held in a local until the window closes. A lock file that
    cannot be created at all is no reason to show no window, so then the
    window opens without the guard. The lock and port paths can be passed
    in, which the tests do so they never meet a window really open.
    </remarks>
    """
    try:
        import webview  # pywebview
    except ImportError:
        log("pywebview is not installed (pip install pywebview); opening in the browser instead")
        webbrowser.open(url)
        return 0
    lock_file = lock_file or instance.lock_path(WINDOW_LOCK_NAME)
    port_file = port_file or instance.lock_path(WINDOW_PORT_NAME)
    try:
        held = instance.claim(lock_file)  # noqa: F841  kept until the window closes
    except instance.AlreadyRunning:
        if ask_window_forward(port_file):
            log("the configuration window is already open; asked it to come forward")
        else:
            log("the configuration window is already open")
        return 0
    try:
        window = webview.create_window(WINDOW_TITLE, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                                       min_size=(900, 600))
        RaiseListener(port_file, window.restore, log).start()
        webview.start(icon=str(WINDOW_ICON) if WINDOW_ICON.is_file() else None)
    except Exception as err:  # no web engine on this machine, for example
        log(f"could not open a window ({err}); opening in the browser instead")
        webbrowser.open(url)
    return 0


def run(host: str, port: int, log=print, browser: bool = False) -> int:
    """<summary>
    The whole ``gui`` command: check the daemon, then show the page.
    </summary>
    <param name="host">Loopback name or address the daemon is bound to.</param>
    <param name="port">The API port from the config file.</param>
    <param name="log">Where notices go.</param>
    <param name="browser">True to skip the native window and hand the URL
    straight to the normal browser.</param>
    <returns>0 once the page has been shown or the window closed, 1 when
    nothing is listening.</returns>
    <remarks>
    The reachability check comes first on purpose. Opening a window on a dead
    port shows an empty frame with a browser error in it, and whoever sees
    that has no way of knowing the daemon is the missing piece, so the
    message names the command that starts one.

    Only http is built here, never https. The daemon serves plain http on
    loopback, where there is nothing between the two to protect against.
    </remarks>
    <see cref="daemon_reachable"/>
    """
    url = f"http://{host}:{port}/"
    if not daemon_reachable(host, port):
        log(f"nothing is listening at {url}. Start the daemon first:  deckplate run")
        return 1
    if browser:
        webbrowser.open(url)
        return 0
    return open_window(url, log)


if __name__ == "__main__":
    sys.exit(run("127.0.0.1", 8765))
