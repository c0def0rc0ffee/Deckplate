"""<summary>
Which window is in front, so the deck can follow it on to the right page.
Reads the focused window from the desktop and watches it for changes, so a
page with a match_window expression can come up by itself when its program is
in front.
</summary>
<remarks>
Reading only. Nothing here focuses, moves, closes or otherwise touches a
window, and nothing here decides which page to show: it reports what is in
front and the controller does the rest.

Two backends, chosen by platform:

* X11, which is what Cinnamon on Mint gives us, by asking xprop for the root
  window's _NET_ACTIVE_WINDOW and then for that window's class and title.
  xprop is part of x11-utils and is present on any normal desktop install.
* Windows, through GetForegroundWindow in user32 with ctypes, plus the
  process name behind the window so a page can match "firefox" rather than
  whatever the title happens to say.

Anywhere else, including a Wayland session, there is no way to ask what is
focused without a compositor specific protocol that most compositors do not
offer to an ordinary program. That is not an error and is not worth a crash:
<see cref="read_window"/> returns None, the watcher logs the reason once, and
pages carry on being switched by hand exactly as before.
</remarks>
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable

# xprop is fast, but a wedged X server would hang the watcher thread for as
# long as we let it. A second is many times longer than the call ever takes.
READ_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class Window:
    """<summary>
    The focused window, reduced to the two things worth matching on.
    </summary>
    <param name="app">The window class on X11, the process name without its
    extension on Windows. Lower case: "firefox", "code", "gnome-terminal".</param>
    <param name="title">The title bar text, which changes as the user works.</param>
    <remarks>
    Both fields default to empty rather than being required, because each
    backend can read one and fail to read the other, and half an answer is
    still worth matching against. Frozen so the watcher thread and the
    controller thread can share one without either editing it.

    Nothing else about the window is kept. Position, size and the window
    handle are all deliberately dropped: this module only reports, and
    holding a handle would invite something to act on it.
    </remarks>
    """

    app: str = ""
    title: str = ""

    @property
    def signature(self) -> tuple[str, str]:
        """<summary>
        The pair the watcher compares to decide the focus has actually moved.
        </summary>
        <returns>(app, title).</returns>
        <remarks>
        The title is in here on purpose, so switching between two windows of
        the same program counts as a move. It also means a program that
        rewrites its own title, a browser following a tab or a player showing
        a track, looks like a focus change every time it does so. The
        controller absorbs that by only acting when the signature picks a
        different page.
        </remarks>
        """
        return self.app, self.title

    def __str__(self) -> str:
        """<summary>One line for the log and for the configuration page.</summary>
        <returns>"app: title", or just the app when there is no title.</returns>
        <remarks>Display only. Never match on this: use
        <see cref="matches"/>, which searches the two fields separately.</remarks>"""
        return f"{self.app}: {self.title}" if self.title else self.app


def matches(pattern: str, window: Window | None) -> bool:
    """<summary>
    Does this window match a page's match_window expression?
    </summary>
    <param name="pattern">A regular expression, already checked at config load.</param>
    <param name="window">The focused window, or None when it is unknown.</param>
    <returns>True when it is found anywhere in the app name or the title. The
    search is case insensitive, and it is a search rather than a full match, so
    "firefox" matches without anyone having to write ".*firefox.*".</returns>
    <remarks>
    Every way of not matching answers False rather than raising: no window,
    an empty pattern, or a pattern that will not compile. A config file is
    typed by hand and an unfollowed page is a far better outcome than a
    daemon that stops, so this stays quiet by design.

    Because it is a search and not a full match, a short pattern is a broad
    one. "code" finds itself in a title mentioning code as well as in the
    editor's own class, so the first page whose pattern matches wins and
    order in the config decides ties.
    </remarks>
    """
    if window is None or not pattern:
        return False
    try:
        expression = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return False
    return bool(expression.search(window.app) or expression.search(window.title))


# X11


def _xprop(*arguments: str) -> str:
    """<summary>
    Run xprop and hand back its output, or empty text if it could not run.
    </summary>
    <param name="arguments">The xprop arguments, without the program name.</param>
    <returns>Standard output, or empty text on any failure at all.</returns>
    <remarks>
    A missing xprop, a non zero exit and a call that outran
    <see cref="READ_TIMEOUT_SECONDS"/> are all the same answer here: empty
    text, which the callers read as "focus unknown". That is deliberate, so
    a desktop without x11-utils installed costs the user automatic pages and
    nothing else.
    </remarks>
    """
    try:
        result = subprocess.run(("xprop", *arguments), capture_output=True, text=True,
                                timeout=READ_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout


def parse_active_window_id(text: str) -> str | None:
    """<summary>
    Pull the window id out of `xprop -root _NET_ACTIVE_WINDOW`.
    </summary>
    <param name="text">The output of that xprop call, or empty text.</param>
    <returns>The id as written, "0x3400007", or None when nothing is focused.
    A desktop with no focused window reports 0x0, which is not a window.</returns>
    <remarks>
    Split out from the xprop call so the parsing can be tested without an X
    server: the tests hand it recorded output. It reads only the first id on
    the line, because the property is a list and the rest are stale entries
    left by window managers that never trim it.
    </remarks>
    """
    for line in text.splitlines():
        if "_NET_ACTIVE_WINDOW" not in line or "#" not in line:
            continue
        first = line.split("#", 1)[1].split(",")[0].strip()
        if first and first != "0x0":
            return first
    return None


def parse_window_properties(text: str) -> Window:
    """<summary>
    Pull the class and title out of `xprop -id <id> WM_CLASS _NET_WM_NAME WM_NAME`.
    </summary>
    <param name="text">The output of that xprop call, or empty text.</param>
    <returns>A window, with either field left empty when it could not be
    read. An empty window means the id named nothing useful, not an error.</returns>
    <remarks>
    WM_CLASS holds two strings, the instance then the class: for Firefox that
    is "Navigator", "firefox". The second is the stable one, so it is preferred
    and the first is the fallback. _NET_WM_NAME is the UTF-8 title and WM_NAME
    the older Latin-1 one; either will do, the newer first.
    </remarks>
    """
    app = ""
    title = ""
    for line in text.splitlines():
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if not value:
            continue
        if name.startswith("WM_CLASS"):
            parts = [part.strip().strip('"') for part in value.split('", "')]
            parts = [part.strip('"') for part in parts if part.strip('"')]
            if parts:
                app = parts[-1].lower()
        elif name.startswith("_NET_WM_NAME") and not title:
            title = value.strip('"')
        elif name.startswith("WM_NAME") and not title:
            title = value.strip('"')
    return Window(app=app, title=title)


def x11_window() -> Window | None:
    """<summary>
    The focused window on an X11 display, read through two xprop calls.
    </summary>
    <returns>The window, or None if it could not be read.</returns>
    <remarks>
    Two calls, not one: the root window names the active window and only
    then can its class and title be asked for. Between the two the focus may
    already have moved, so the answer is a moment old by definition. That is
    harmless at a five times a second poll and is why nothing here tries to
    make the pair atomic.

    A window with neither class nor title is reported as None rather than as
    an empty window, so a desktop showing only a wallpaper does not match a
    page with a loose pattern.
    </remarks>
    """
    window_id = parse_active_window_id(_xprop("-root", "-notype", "_NET_ACTIVE_WINDOW"))
    if window_id is None:
        return None
    text = _xprop("-id", window_id, "-notype", "WM_CLASS", "_NET_WM_NAME", "WM_NAME")
    if not text:
        return None
    window = parse_window_properties(text)
    return window if window.app or window.title else None


# Windows


def windows_window() -> Window | None:
    """<summary>
    The focused window on Windows, read through user32 and kernel32.
    </summary>
    <returns>The window, or None if it could not be read.</returns>
    <remarks>
    Only ever reads: GetForegroundWindow for the handle, GetWindowTextW for the
    title, and the process image name for the app. The process handle is asked
    for with PROCESS_QUERY_LIMITED_INFORMATION, the narrowest right that answers
    the question, which also works against processes running as another user.

    Both ctypes blocks swallow every exception rather than naming the ones
    they expect. Being imported on a machine that is not Windows, a build
    without ctypes and any API refusing to answer all mean the same thing to
    the caller, which is that the focus is unknown. The process handle is
    closed in a finally, so a failure part way through does not leak it.
    </remarks>
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # not Windows, or a build without ctypes
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = user32.GetForegroundWindow()
        if not handle:
            return None
        length = user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        title = buffer.value

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        app = ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if process:
            try:
                size = wintypes.DWORD(260)
                name = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size)):
                    app = os.path.basename(name.value).removesuffix(".exe").lower()
            finally:
                kernel32.CloseHandle(process)
    except Exception:  # any Windows API surprise: no focus rather than a crash
        return None
    if not app and not title:
        return None
    return Window(app=app, title=title)


# Dispatch


def unsupported_reason() -> str | None:
    """<summary>
    Why the focused window cannot be read here.
    </summary>
    <returns>A sentence to log, or None when this desktop is supported.</returns>
    <remarks>
    Written as a sentence rather than a code because it is shown to the user
    and it has to explain that nothing is broken. A Wayland session is the
    common answer on a modern desktop, and the right response is to switch
    the session to X11 or to carry on switching pages by hand.

    None is not a promise that reading will work. It says only that the
    session is one of the two supported kinds, and the read can still come
    back empty.
    </remarks>
    """
    if sys.platform == "win32":
        return None
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        return ("this is a Wayland session, which does not let a program ask which window "
                "is focused; pages stay manual")
    if not os.environ.get("DISPLAY"):
        return "there is no X display to ask; pages stay manual"
    return None


def read_window() -> Window | None:
    """<summary>
    The focused window now, whichever backend this platform calls for.
    </summary>
    <returns>The window, or None when it is unknown or unreadable.</returns>
    <remarks>
    The one call the rest of the daemon should use, and the default the
    watcher is built with. It blocks: on X11 it runs two subprocesses, which
    is why the watcher keeps it off the controller thread.
    </remarks>
    """
    if sys.platform == "win32":
        return windows_window()
    if unsupported_reason() is not None:
        return None
    return x11_window()


class FocusWatcher:
    """<summary>
    Polls the focused window on its own thread and holds the latest answer.
    </summary>
    <remarks>
    On its own thread on purpose. The poll is a couple of subprocess calls, and
    although they take a few milliseconds, doing them on the controller thread
    would put the deck's whole loop behind an external program five times a
    second. Here the controller only ever reads a variable.

    The thread is a daemon and stops on its own when the daemon exits, and
    <see cref="stop"/> waits for it so a config reload does not leave two
    watchers polling at once.
    </remarks>
    """

    def __init__(self, poll_seconds: float = 0.2, *,
                 read: Callable[[], "Window | None"] = read_window,
                 log: Callable[[str], None] = print) -> None:
        """<summary>Set a watcher up without starting its thread.</summary>
        <param name="poll_seconds">How long to wait between reads. Floored at
        0.05 whatever is asked for, because the X11 read is two subprocesses
        and a config asking for a millisecond would spend the machine on
        it.</param>
        <param name="read">What to call for one reading. Swapped for a stub in
        the tests, so none of this needs a desktop to exercise.</param>
        <param name="log">Where a one off complaint goes.</param>
        <remarks>
        Nothing is read here and no thread is started, so constructing a
        watcher is free and safe on any platform. <see cref="start"/> is what
        decides whether this desktop can answer at all.
        </remarks>
        """
        self.poll_seconds = max(0.05, poll_seconds)
        self.read = read
        self.log = log
        self._window: Window | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._complained = False

    @property
    def current(self) -> Window | None:
        """<summary>
        The most recently read focused window, or None if unknown.
        </summary>
        <returns>The window as of the last poll, which may be up to
        ``poll_seconds`` old.</returns>
        <remarks>
        The only thing the controller touches, and it never blocks: the read
        itself happens on the watcher's thread. None covers both "not read
        yet" and "this desktop cannot say", so a caller must not treat it as
        a change in focus.
        </remarks>
        """
        with self._lock:
            return self._window

    def poll_once(self) -> Window | None:
        """<summary>
        Read the focused window now and remember it.
        </summary>
        <returns>What was read, or None when the read failed.</returns>
        <remarks>
        The whole of the polling work, kept separate so the tests can drive
        the watcher a step at a time with no thread involved.

        A read that raises is swallowed and complained about once, not once
        per poll: a broken desktop would otherwise write five lines a second
        to the log for as long as the daemon runs. The remembered window is
        left as it was, so the last good answer stands rather than the page
        flapping back to the fallback.
        </remarks>
        """
        try:
            window = self.read()
        except Exception as err:  # a broken desktop must not take the daemon with it
            if not self._complained:
                self._complained = True
                self.log(f"cannot tell which window is focused: {err}")
            return None
        with self._lock:
            self._window = window
        return window

    def start(self) -> "FocusWatcher":
        """<summary>
        Start polling on a background thread, if this desktop can answer.
        </summary>
        <returns>The watcher itself, so it can be started where it is made.</returns>
        <remarks>
        Safe to call twice: a watcher that already has a thread returns
        itself untouched, which is what lets a config reload call start on
        every reload without counting how many are running.

        On an unsupported desktop it logs the reason and returns without a
        thread, and <see cref="current"/> then stays None for ever. That is
        the supported outcome, not a failure, so it does not raise.
        </remarks>
        """
        reason = unsupported_reason()
        if reason is not None:
            self.log(f"not following the focused window: {reason}")
            return self
        if self._thread is not None:
            return self
        # Cleared here rather than in stop(): a watcher stopped by a config
        # reload and started again by the next one has to be able to run.
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="focus", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        """
        <summary>
        Thread body: poll the focused window every poll_seconds until stopped.
        </summary>
        """
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(self.poll_seconds)

    def stop(self) -> None:
        """<summary>
        Stop polling and wait for the thread to finish.
        </summary>
        <remarks>
        Waits up to two seconds, which is far longer than a poll takes, so
        in practice the thread is gone when this returns. It is a bounded
        wait rather than an open one because the thread may be inside an
        xprop call against a wedged X server, and shutdown must not hang on
        that.

        Safe on a watcher that was never started, and safe to call twice.
        Starting again afterwards works: the stop flag is cleared by
        <see cref="start"/>, not here.
        </remarks>
        """
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)
