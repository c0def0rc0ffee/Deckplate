"""<summary>
Run the action behind a key.
</summary>
<remarks>
The runner only knows how to do things on the PC (hotkeys, programs, URLs)
and delegates anything about the deck itself (pages, brightness, sleep) to a
context object, which in practice is the Controller. Every dependency is
injectable so the tests never send a real keystroke or start a real program.

Nothing here speaks to the deck. That separation is deliberate: an action is
user supplied configuration, and configuration must never be able to reach
the USB layer, so the only deck side effects available are the three named on
<see cref="ActionContext"/>.

Dispatch is by string, matching the ``type`` field written in the config file,
so the set of types here and the set the config validator accepts have to be
kept in step. An unknown type raises rather than being ignored, because a
silently skipped action looks to the user like a dead key.
</remarks>
"""

from __future__ import annotations

import os
import random
import shlex
import subprocess
import sys
import time
import webbrowser
from typing import Callable, Protocol

from . import hotkeys
from .config import Action
from .holds import HoldManager


class ActionContext(Protocol):
    """<summary>
    The only three things an action is allowed to do to the deck itself.
    </summary>
    <remarks>
    Implemented by the Controller in normal running and by a recorder in the
    tests. Kept to three calls on purpose: everything the deck can be told to
    do from a key binding has to pass through here, so widening this interface
    is the moment to stop and think rather than a routine edit.

    All three are called from the action thread, not from the reader thread,
    so an implementation has to be safe to call while the deck is being read.
    </remarks>
    """

    def switch_page(self, target: str) -> None:
        """<summary>Show another page of keys.</summary>
        <param name="target">A page name, or one of the relative words the
        controller understands such as next or back.</param>
        <remarks>An unknown name is the controller's problem to report, not
        this module's: the runner passes the string through untouched.</remarks>"""
        ...

    def set_brightness(self, value: int | None = None, delta: int | None = None) -> None:
        """<summary>Set the backlight to a level, or step it by an amount.</summary>
        <param name="value">An absolute level, or None to step instead.</param>
        <param name="delta">A step up or down, or None when setting a level.</param>
        <remarks>Both may be None when the config said neither, so the
        implementation has to tolerate a call that asks for nothing.</remarks>"""
        ...

    def sleep_deck(self) -> None:
        """<summary>Blank the screens until the next key press wakes them.</summary>
        <remarks>Sleep is a screen state, not a power state: the deck keeps
        reporting presses while it is asleep, which is what wakes it.</remarks>"""
        ...


def default_launch(command: str) -> None:
    """<summary>
    Start a program and do not wait for it.
    </summary>
    <param name="command">The command line as written in the config file.</param>
    <remarks>
    Deliberately fire and forget: the daemon must not block on a program that
    runs for hours, and it never collects an exit status, so a command that
    fails to start raises here while one that starts and then dies is invisible.

    The two platforms split the string differently. Windows is handed the line
    whole because it does its own parsing, and Linux is split with shlex so
    quoting behaves as it does in a shell. No shell is spawned either way, so
    pipes and redirection in a command will not work.

    ``start_new_session`` on Linux detaches the child from the daemon's process
    group, so the program survives the daemon being stopped and does not take a
    Ctrl C meant for the daemon.
    </remarks>
    """
    if os.name == "nt":
        subprocess.Popen(command, close_fds=True)
    else:
        subprocess.Popen(shlex.split(command), start_new_session=True)


def platform_command(params: dict) -> str:
    """<summary>
    The command for this platform, falling back to the shared one.
    </summary>
    <param name="params">A launch action's params, holding ``command`` and
    optionally ``command_windows`` and ``command_linux``.</param>
    <returns>The command line to run here.</returns>
    <remarks>
    One config file is carried between the Linux and Windows machines, so a
    key that launches a program needs a way to say both paths. An empty
    override counts as absent, which lets a per platform value be blanked in
    the configuration page without deleting the key.
    </remarks>
    
    <exception cref="KeyError">No ``command`` key and no override for this
    platform, which means the config was written wrongly.</exception>"""
    if sys.platform == "win32":
        return params.get("command_windows") or params["command"]
    return params.get("command_linux") or params["command"]


class ActionRunner:
    """<summary>
    Turns one configured action into something actually happening.
    </summary>
    <remarks>
    Every way this class can reach the outside world arrives as a constructor
    argument, so a test can pass recorders for all of them and nothing real is
    pressed, launched or opened. That is the only reason the argument list is
    as long as it is.

    The runner is stateless apart from the hold manager, which owns the
    threads behind the toggling actions and outlives any single press.

    <see cref="run"/> is the entry point and swallows every failure. Call
    <see cref="_dispatch"/> directly only from inside the class: it raises.
    </remarks>
    """

    def __init__(self, context: ActionContext, *,
                 send_hotkey: Callable[[str], None] = hotkeys.send,
                 launch: Callable[[str], None] = default_launch,
                 open_url: Callable[[str], None] = lambda url: webbrowser.open(url),
                 sleep: Callable[[float], None] = time.sleep,
                 holds: HoldManager | None = None,
                 press_keys: Callable[[str], None] = hotkeys.press,
                 release_keys: Callable[[str], None] = hotkeys.release,
                 rng: Callable[[float, float], float] = random.uniform,
                 log: Callable[[str], None] = print) -> None:
        """<summary>
        Build a runner, taking its deck context and its way out to the machine.
        </summary>
        <param name="context">Who to ask for anything involving the deck.</param>
        <param name="holds">The manager owning the toggling threads. Pass the
        controller's own so that a hold started on one page can be stopped from
        another; leaving it None makes a private one, which is right for a test
        and wrong for the daemon.</param>
        <param name="log">Where a failed action goes. Defaults to print because
        the daemon runs in the foreground under its unit.</param>
        <remarks>
        Every other argument is a seam for the tests: the hotkey senders, the
        launcher, the URL opener, the sleep and the random source. The defaults
        are the real implementations, so constructing this with only a context
        gives a runner that genuinely presses keys.

        The defaults are bound at class definition time, so patching
        <see cref="hotkeys.send"/> after a runner exists will not affect it.
        Pass the replacement in instead.
        </remarks>
        """
        self.context = context
        self.send_hotkey = send_hotkey
        self.launch = launch
        self.open_url = open_url
        self.sleep = sleep
        self.holds = holds or HoldManager(log=log)
        self.press_keys = press_keys
        self.release_keys = release_keys
        self.rng = rng
        self.log = log

    def run(self, action: Action) -> bool:
        """<summary>
        Run one action. Failures are logged and reported as False, never raised.
        </summary>
        <param name="action">One action, already validated by the config layer.</param>
        <returns>True when it ran, False when it raised.</returns>
        <remarks>
        Catching everything is the point of this method. A mistyped hotkey, a
        program that is not installed or a display that has gone away must not
        take the daemon down with them, because the deck would then stop
        responding entirely over one bad key.

        True only means nothing raised. A launched program that exits at once,
        or a hotkey sent to a window that ignores it, still reports True: there
        is nothing here that could know otherwise.
        </remarks>
        """
        try:
            self._dispatch(action)
            return True
        except Exception as err:  # a bad hotkey or missing program must not kill the daemon
            self.log(f"action {action.type} failed: {err}")
            return False

    def _dispatch(self, action: Action) -> None:
        """<summary>
        Do whatever the action's type says, letting anything that goes wrong out.
        </summary>
        <param name="action">The action to carry out.</param>
        <remarks>
        Kept apart from <see cref="run"/> so that the multi type can recurse
        into it: a step inside a multi must be able to fail the whole multi
        rather than being caught and counted as a success.

        Three of the types (hold, boost, repeat) are toggles, not one shot
        actions. They hand over to the hold manager and return at once, leaving
        a thread running, so a second press of the same key is what stops them.

        The hold, boost and repeat branches return quietly when given no keys.
        That is the half filled in binding case, and doing nothing is kinder
        than starting a thread that presses an empty combination.
        </remarks>
        
        <exception cref="ValueError">The type is not one this module knows.</exception>"""
        params = action.params
        if action.type == "hotkey":
            if params["keys"]:
                hold_ms = params.get("hold_ms", 0)
                if hold_ms:
                    # Held for a set time, for a game that wants the key down
                    # rather than tapped. Released even if the sleep is cut short.
                    self.press_keys(params["keys"])
                    try:
                        self.sleep(hold_ms / 1000)
                    finally:
                        self.release_keys(params["keys"])
                else:
                    self.send_hotkey(params["keys"])
        elif action.type == "sequence":
            delay = params.get("delay_ms", 0) / 1000
            for index, combo in enumerate(params["keys"]):
                if index and delay:
                    self.sleep(delay)
                self.send_hotkey(combo)
        elif action.type == "chord":
            self._chord(params)
        elif action.type == "hold":
            if not params["keys"]:
                return
            self.holds.toggle(params["keys"],
                              (params["hold_min_ms"], params["hold_max_ms"]),
                              (params["release_min_ms"], params["release_max_ms"]))
        elif action.type == "boost":
            if not (params["hold"] or params["keys"]):
                return
            self.holds.toggle_boost(params["hold"], params["keys"],
                                    params["on_ms"], params["off_ms"])
        elif action.type == "repeat":
            if not params["keys"]:
                return
            self.holds.toggle_repeat(params["keys"], params["every_ms"])
        elif action.type == "launch":
            self.launch(platform_command(params))
        elif action.type == "url":
            self.open_url(params["url"])
        elif action.type == "page":
            self.context.switch_page(params["page"])
        elif action.type == "brightness":
            self.context.set_brightness(params.get("value"), params.get("delta"))
        elif action.type == "sleep":
            self.context.sleep_deck()
        elif action.type == "multi":
            delay = params.get("delay_ms", 0) / 1000
            for index, step in enumerate(params["steps"]):
                if index and delay:
                    self.sleep(delay)
                self._dispatch(step)
        else:
            raise ValueError(f"unknown action type {action.type}")

    def _chord(self, params: dict) -> None:
        """<summary>
        Hold one key down, tap the others with a random pause before each, let go.
        </summary>
        <param name="params">A chord action's params: ``hold``, ``keys`` and the
        ``delay_min_ms`` and ``delay_max_ms`` bounds for the pause before each tap.</param>
        <remarks>
        The held key stays down for the whole run; only the taps are paced. The
        pause comes before each tap including the first, which gives the held
        key a moment to register before anything else arrives.

        A zero upper bound skips the pause entirely rather than sleeping for
        nothing, so a chord can be sent as fast as the backend will take it.

        This blocks for the length of the whole chord, which is the caller's
        thread. A long chord therefore delays the next press, and the try and
        finally is what guarantees the held key is let go if a tap raises part
        way through.
        </remarks>
        """
        hold = params.get("hold") or ""
        low, high = params["delay_min_ms"], params["delay_max_ms"]
        if hold:
            self.press_keys(hold)
        try:
            for combo in params["keys"]:
                if high > 0:
                    self.sleep(self.rng(low, high) / 1000)
                self.send_hotkey(combo)
        finally:
            if hold:
                self.release_keys(hold)
