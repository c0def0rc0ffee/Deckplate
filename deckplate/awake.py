"""<summary>
The two desktop toggles a key can hold on: Stay Awake, which keeps the
machine from going idle, and Do Not Disturb, which keeps notifications quiet.
</summary>
<remarks>
Both run as threads owned by the hold manager, like a hold or a repeat, so a
key shows them as running with its active face and a second press stops
them, and a config reload releases them with everything else.

Stay Awake does two things, each optional. The honest one asks the desktop
not to go idle through the screensaver inhibit interface, which is what a
"caffeine" tray program does and which works on X11 and on Wayland alike.
The other nudges the pointer one pixel and back on a timer through the X
test extension, which fools programs that watch for input rather than for
the idle timer, Teams among them. Under Wayland the nudge only reaches X
programs, so the inhibit is the half that matters there.

Do Not Disturb asks the notification server to hold notifications through
the Inhibit call of the desktop notification specification. KDE Plasma shows
its own do not disturb state while it is held and releases it when the
inhibit is released, or when the daemon's bus connection goes away.

Every outside effect is a constructor argument, so the tests run the threads
with recorders and nothing on the real desktop changes.
</remarks>
"""

from __future__ import annotations

import threading
from typing import Callable

from . import sessionbus

SCREENSAVER = ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver")
NOTIFICATIONS = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications")
APP_NAME = "deckplate"
NUDGE_DEFAULT_MS = 60_000


class ScreenSaverInhibitor:
    """<summary>The desktop's screensaver inhibit, taken and given back by cookie.</summary>"""

    def inhibit(self, reason: str) -> int:
        """<summary>Ask the desktop not to go idle.</summary>
        <param name="reason">Shown by the desktop beside the daemon's name.</param>
        <returns>The cookie to release with.</returns>
        <exception cref="sessionbus.BusError">No bus or no screensaver service.</exception>"""
        name, path, interface = SCREENSAVER
        (cookie,) = sessionbus.call(name, path, interface, "Inhibit", "(ss)", (APP_NAME, reason))
        return int(cookie)

    def release(self, cookie: int) -> None:
        """<summary>Let the desktop go idle again.</summary>
        <param name="cookie">From <see cref="inhibit"/>.</param>
        <exception cref="sessionbus.BusError">The release failed.</exception>"""
        name, path, interface = SCREENSAVER
        sessionbus.call(name, path, interface, "UnInhibit", "(u)", (cookie,))


class NotificationInhibitor:
    """<summary>The notification server's inhibit, taken and given back by key.</summary>"""

    def inhibit(self, reason: str) -> int:
        """<summary>Ask the notification server to hold notifications.</summary>
        <param name="reason">Shown by the desktop as why.</param>
        <returns>The key to release with.</returns>
        <exception cref="sessionbus.BusError">No bus, or a server without Inhibit.</exception>"""
        name, path, interface = NOTIFICATIONS
        (key,) = sessionbus.call(name, path, interface, "Inhibit", "(ssa{sv})", (APP_NAME, reason, {}))
        return int(key)

    def release(self, key: int) -> None:
        """<summary>Let notifications through again.</summary>
        <param name="key">From <see cref="inhibit"/>.</param>
        <exception cref="sessionbus.BusError">The release failed.</exception>"""
        name, path, interface = NOTIFICATIONS
        sessionbus.call(name, path, interface, "UnInhibit", "(u)", (key,))


def xtest_nudge() -> None:
    """<summary>
    Move the pointer one pixel right and back again through the X test extension.
    </summary>
    <remarks>
    Two relative motion events, so the pointer ends where it started and the
    user never sees it move. A fresh Display connection per nudge, opened and
    closed here, because a nudge is a once a minute thing and sharing the
    hotkey backend's connection would need its lock and its lifetime.
    </remarks>
    <exception cref="Exception">No X display, or no XTEST extension.</exception>"""
    from Xlib import X
    from Xlib import display as xdisplay
    from Xlib.ext import xtest
    display = xdisplay.Display()
    try:
        xtest.fake_input(display, X.MotionNotify, True, x=1, y=0)
        display.sync()
        xtest.fake_input(display, X.MotionNotify, True, x=-1, y=0)
        display.sync()
    finally:
        display.close()


class Inhibit(threading.Thread):
    """<summary>
    A thread that holds a desktop inhibit for as long as it runs, nudging
    on a timer if asked, and releases it on stop.
    </summary>
    <remarks>
    One class serves both toggles: the inhibitor it is given decides which
    service is held, and the nudge is only ever given for Stay Awake. The
    inhibit is taken on the thread, not in the constructor, so building one
    touches nothing and a failure to take it is logged rather than raised
    into the key press.

    A failed inhibit does not stop the thread when there is a nudge to run:
    on a desktop without the service the nudge is still worth having. With
    neither the thread ends at once and the key shows as off.
    </remarks>
    """

    def __init__(self, name: str, *, inhibitor=None, reason: str = "Deckplate key",
                 nudge: Callable[[], None] | None = None, every_ms: int = NUDGE_DEFAULT_MS,
                 log: Callable[[str], None] = print) -> None:
        """<summary>Set up the thread. Nothing is taken until it is started.</summary>
        <param name="name">The task name, for the thread and the log.</param>
        <param name="inhibitor">An object with ``inhibit(reason) -> int`` and
        ``release(int)``, or None to hold nothing.</param>
        <param name="reason">Given to the desktop as why.</param>
        <param name="nudge">Called every ``every_ms`` while running, or None.</param>
        <param name="every_ms">The nudge interval.</param>
        <param name="log">Where failures go.</param>"""
        super().__init__(name=name, daemon=True)
        self.inhibitor = inhibitor
        self.reason = reason
        self.nudge = nudge
        self.every_ms = every_ms
        self.log = log
        self._stop_event = threading.Event()
        self.cookie: int | None = None
        self.nudges = 0

    def stop(self) -> None:
        """<summary>Ask the thread to release and finish.</summary>"""
        self._stop_event.set()

    def run(self) -> None:
        """<summary>The thread body: take the inhibit, nudge until stopped, release.</summary>
        <remarks>Do not call this directly. Use start.</remarks>"""
        if self.inhibitor is not None:
            try:
                self.cookie = self.inhibitor.inhibit(self.reason)
            except Exception as err:
                self.log(f"{self.name}: inhibit not taken ({err})")
        if self.cookie is None and self.nudge is None:
            return  # nothing held and nothing to do: the key reads as off
        try:
            while not self._stop_event.is_set():
                if self.nudge is not None:
                    try:
                        self.nudge()
                        self.nudges += 1
                    except Exception as err:
                        self.log(f"{self.name}: nudge failed ({err}); no more nudges")
                        self.nudge = None
                        if self.cookie is None:
                            return
                if self._stop_event.wait(self.every_ms / 1000):
                    break
        finally:
            if self.cookie is not None and self.inhibitor is not None:
                try:
                    self.inhibitor.release(self.cookie)
                except Exception as err:
                    self.log(f"{self.name}: release failed ({err})")
                self.cookie = None


def stay_awake(params: dict, *, inhibitor=None, nudge=None, log: Callable[[str], None] = print) -> Inhibit:
    """<summary>Build the Stay Awake thread from a stay_awake action's params.</summary>
    <param name="params">``inhibit`` and ``nudge`` booleans and ``every_ms``.</param>
    <param name="inhibitor">The screensaver inhibitor, or None for the real one.</param>
    <param name="nudge">The nudge callable, or None for the X test one.</param>
    <param name="log">Where failures go.</param>
    <returns>The thread, not yet started.</returns>"""
    holder = (inhibitor or ScreenSaverInhibitor()) if params.get("inhibit", True) else None
    mover = (nudge or xtest_nudge) if params.get("nudge", True) else None
    return Inhibit("stay awake", inhibitor=holder, reason="Stay Awake key on the deck",
                   nudge=mover, every_ms=params.get("every_ms", NUDGE_DEFAULT_MS), log=log)


def do_not_disturb(params: dict, *, inhibitor=None, log: Callable[[str], None] = print) -> Inhibit:
    """<summary>Build the Do Not Disturb thread from a dnd action's params.</summary>
    <param name="params">Unused today; kept so every builder has the same shape.</param>
    <param name="inhibitor">The notification inhibitor, or None for the real one.</param>
    <param name="log">Where failures go.</param>
    <returns>The thread, not yet started.</returns>"""
    del params
    return Inhibit("do not disturb", inhibitor=inhibitor or NotificationInhibitor(),
                   reason="Do Not Disturb key on the deck", every_ms=3_600_000, log=log)
