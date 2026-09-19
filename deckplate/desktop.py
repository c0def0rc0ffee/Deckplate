"""<summary>
Acting on other programs' windows: bring one to the front, minimise, maximise
or close it.
</summary>
<remarks>
The reading half of this, which window is focused, lives in the focus module
and is deliberately read only. This module is the writing half, kept apart so
the watcher thread never has anything that could act.

Linux goes through xdotool, with wmctrl for the one thing xdotool cannot do,
which is maximise. Both are X11 tools and neither works on Wayland, the same
limit every keystroke here has. Windows goes through user32 directly and
needs nothing installed.

A window is found by a regular expression searched, ignoring case, in its
class and its title, the same way a page's match_window works, so a pattern
that picks a page also picks that page's window.
</remarks>
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from typing import Callable

OPERATIONS = ("focus", "minimise", "maximise", "close")


class DesktopError(RuntimeError):
    """<summary>
    A window could not be acted on, with the reason written for the log.
    </summary>
    """

    pass


def xdotool_search_args(pattern: str, field: str) -> list[str]:
    """<summary>
    The xdotool line that lists visible windows whose field matches a pattern.
    </summary>
    <param name="pattern">A regular expression; xdotool takes one as it is.</param>
    <param name="field">``class``, ``classname`` or ``name``.</param>
    <returns>The argument list, xdotool included.</returns>
    <remarks>
    Split out so it can be asserted without xdotool installed. Only visible
    windows are asked for, because a hidden helper window of the same class
    would otherwise be found first and activated to no visible effect.
    </remarks>
    """
    return ["xdotool", "search", "--onlyvisible", f"--{field}", pattern]


class XdotoolDesktop:
    """<summary>
    Window actions on X11 through xdotool and wmctrl.
    </summary>
    <remarks>
    Every action is one or two short processes and blocks until they exit,
    which on X11 is a few milliseconds. The search is run three times, once
    per field, and the first window found wins, so a pattern that matches
    both a class and a title picks the class match.
    </remarks>
    """

    def __init__(self, run: Callable = subprocess.run, which: Callable = shutil.which) -> None:
        """<summary>Build over a runner and a path lookup, both replaceable in tests.</summary>
        <param name="run">Called like subprocess.run.</param>
        <param name="which">Called like shutil.which, to find wmctrl.</param>"""
        self._run = run
        self._which = which

    def _xdotool(self, *args: str, check: bool = True) -> str:
        """<summary>Run one xdotool command.</summary>
        <param name="args">The arguments after ``xdotool``.</param>
        <param name="check">Raise on a non zero exit. Off for a search, which
        exits 1 when it simply found nothing.</param>
        <returns>Standard output, stripped.</returns>
        <exception cref="DesktopError">xdotool is missing or refused.</exception>"""
        try:
            result = self._run(["xdotool", *args], capture_output=True, text=True, timeout=5, check=check)
        except FileNotFoundError as err:
            raise DesktopError("xdotool is not installed (apt install xdotool)") from err
        except subprocess.CalledProcessError as err:
            detail = (err.stderr or "").strip() or f"exit {err.returncode}"
            raise DesktopError(f"xdotool {args[0]} failed: {detail}") from err
        return (result.stdout or "").strip()

    def find(self, pattern: str) -> str | None:
        """<summary>The id of the first visible window matching the pattern, or None.</summary>
        <param name="pattern">A regular expression for the class or the title.</param>
        <returns>An X window id as text, or None when nothing matched.</returns>"""
        for field in ("class", "classname", "name"):
            text = self._xdotool(*xdotool_search_args(pattern, field)[1:], check=False)
            ids = [line for line in text.splitlines() if line.strip()]
            if ids:
                return ids[0].strip()
        return None

    def focus(self, window_id: str) -> None:
        """<summary>Bring a window to the front and give it the keyboard.</summary>
        <param name="window_id">From <see cref="find"/>.</param>"""
        self._xdotool("windowactivate", "--sync", window_id)

    def minimise(self, window_id: str) -> None:
        """<summary>Iconify a window.</summary>
        <param name="window_id">From <see cref="find"/>.</param>"""
        self._xdotool("windowminimize", window_id)

    def maximise(self, window_id: str) -> None:
        """<summary>Maximise a window, through wmctrl, and bring it to the front.</summary>
        <param name="window_id">From <see cref="find"/>.</param>
        <exception cref="DesktopError">wmctrl is not installed.</exception>"""
        if not self._which("wmctrl"):
            raise DesktopError("maximising a window needs wmctrl (apt install wmctrl)")
        try:
            self._run(["wmctrl", "-i", "-r", window_id, "-b", "add,maximized_vert,maximized_horz"],
                      capture_output=True, text=True, timeout=5, check=True)
        except subprocess.CalledProcessError as err:
            raise DesktopError(f"wmctrl failed: {(err.stderr or '').strip() or err.returncode}") from err
        self.focus(window_id)

    def close(self, window_id: str) -> None:
        """<summary>Ask a window to close, as the close button would.</summary>
        <param name="window_id">From <see cref="find"/>.</param>
        <remarks>A request, not a kill: a program with unsaved work shows its
        own prompt, which is the point of going through the window.</remarks>"""
        self._xdotool("windowclose", window_id)


class WindowsDesktop:
    """<summary>
    Window actions on Windows through user32.
    </summary>
    <remarks>
    Windows are enumerated on every call, which is a few hundred handles and
    takes no time worth measuring. Only top level, visible, titled windows
    are considered, which is roughly what the taskbar shows.

    Bringing a window to the front is the call Windows most likes to refuse:
    a process that is not in the foreground may not steal focus. Restoring
    the window first and sending the empty key press the usual workaround
    uses gets most programs through; one that still refuses is a limit of
    Windows, not something to fight harder.
    </remarks>
    """

    def _user32(self):
        """<summary>The user32 library, loaded fresh.</summary>
        <returns>A ctypes WinDLL.</returns>
        <exception cref="DesktopError">Not on Windows.</exception>"""
        try:
            import ctypes
            return ctypes.WinDLL("user32", use_last_error=True)
        except Exception as err:  # not Windows, or no ctypes
            raise DesktopError("window actions here need Windows or an X11 session") from err

    def find(self, pattern: str) -> int | None:
        """<summary>The handle of the first visible titled window matching the pattern.</summary>
        <param name="pattern">A regular expression for the class or the title.</param>
        <returns>A window handle, or None.</returns>"""
        import ctypes
        from ctypes import wintypes

        user32 = self._user32()
        try:
            expression = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return None
        found: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(handle, _param):
            """<summary>Keep the first window whose class or title matches.</summary>"""
            if found or not user32.IsWindowVisible(handle):
                return True
            length = user32.GetWindowTextLengthW(handle)
            if length == 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(handle, title, length + 1)
            klass = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(handle, klass, 256)
            if expression.search(klass.value) or expression.search(title.value):
                found.append(handle)
            return True

        user32.EnumWindows(callback_type(visit), 0)
        return found[0] if found else None

    def focus(self, handle: int) -> None:
        """<summary>Restore a window if it is minimised and bring it to the front.</summary>
        <param name="handle">From <see cref="find"/>.</param>"""
        user32 = self._user32()
        SW_RESTORE = 9
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, SW_RESTORE)
        # The empty key press that lets a background process take the foreground.
        user32.keybd_event(0, 0, 0, 0)
        user32.SetForegroundWindow(handle)

    def minimise(self, handle: int) -> None:
        """<summary>Minimise a window.</summary>
        <param name="handle">From <see cref="find"/>.</param>"""
        self._user32().ShowWindow(handle, 6)  # SW_MINIMIZE

    def maximise(self, handle: int) -> None:
        """<summary>Maximise a window and bring it to the front.</summary>
        <param name="handle">From <see cref="find"/>.</param>"""
        self._user32().ShowWindow(handle, 3)  # SW_MAXIMIZE
        self.focus(handle)

    def close(self, handle: int) -> None:
        """<summary>Ask a window to close, as the close button would.</summary>
        <param name="handle">From <see cref="find"/>.</param>"""
        WM_CLOSE = 0x0010
        self._user32().PostMessageW(handle, WM_CLOSE, 0, 0)


def default_desktop():
    """<summary>
    The desktop for this machine.
    </summary>
    <returns>A Windows desktop on Windows, otherwise an xdotool one.</returns>
    """
    if sys.platform == "win32":
        return WindowsDesktop()
    return XdotoolDesktop()


def act(pattern: str, operation: str, desktop=None) -> bool:
    """<summary>
    Find the window a pattern names and do one thing to it.
    </summary>
    <param name="pattern">A regular expression for the class or the title.</param>
    <param name="operation">One of OPERATIONS.</param>
    <param name="desktop">A desktop for this call, or None for this machine's.</param>
    <returns>True when a window was found and acted on, False when nothing
    matched, so the caller can launch the program instead.</returns>
    <remarks>
    Not found is an answer, not an error, because "focus it or start it" is
    the everyday use of a window key and the runner needs to tell the two
    apart. A tool that is missing or refuses does raise.
    </remarks>

    <exception cref="DesktopError">The desktop could not be driven.</exception>
    <exception cref="ValueError">The operation is not one this module knows.</exception>"""
    if operation not in OPERATIONS:
        raise ValueError(f"unknown window operation {operation}")
    desktop = desktop or default_desktop()
    window = desktop.find(pattern)
    if window is None:
        return False
    getattr(desktop, operation)(window)
    return True
