"""<summary>
Run the action behind a key.
</summary>
<remarks>
The runner only knows how to do things on the PC (hotkeys, typed text,
programs, URLs, web requests, the sound, other programs' windows) and
delegates anything about the deck itself (pages, brightness, sleep) to a
context object, which in practice is the Controller. Every dependency is
injectable so the tests never send a real keystroke or start a real program.

The four positional types, toggle, timer, stopwatch and counter, act on the
key they sit on and never reach here: the controller runs those itself and
hands only their inner actions to the runner.

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

import requests

from . import audio as audio_module
from . import desktop as desktop_module
from . import hotkeys
from .audio import AudioError
from .config import POSITIONAL_ACTIONS, Action
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


def read_token(path: str) -> str:
    """<summary>
    The Authorization header value held in a token file.
    </summary>
    <param name="path">The file, as written in the config. A ``~`` is expanded.</param>
    <returns>The header value: ``Bearer`` and the token, or the file's text as
    it stands when it already names a scheme.</returns>
    <remarks>
    Read on every press rather than once at load, so a rotated token is picked
    up without a restart and so the token never sits inside a Config object
    that the page or a log might print.

    The rule for the scheme is the presence of a space. A bare token gets
    ``Bearer`` in front, which is what nearly every HTTP API wants. Text with a
    space in it, such as ``Basic abc123``, is taken to be the whole header
    value already, so any scheme can be used without another config key.
    Surrounding whitespace and a trailing newline are stripped, because every
    editor leaves one.
    </remarks>
    
    <exception cref="OSError">The file cannot be read.</exception>
    <exception cref="ValueError">The file is empty.</exception>"""
    with open(os.path.expanduser(path), encoding="utf-8") as handle:
        value = handle.read().strip()
    if not value:
        raise ValueError(f"token file is empty: {path}")
    return value if " " in value else f"Bearer {value}"


def default_request(method: str, url: str, headers: dict, body: str | None, timeout: float) -> None:
    """<summary>
    Send one HTTP request and wait for the answer.
    </summary>
    <param name="method">The verb, already checked by the config layer.</param>
    <param name="url">Where to send it.</param>
    <param name="headers">The headers to send, Authorization included when there is one.</param>
    <param name="body">The request body as text, or None for none.</param>
    <param name="timeout">How long to wait for the connection and the answer, in seconds.</param>
    <remarks>
    The answer's content is thrown away. A key that fires a request has no
    way to show what came back, so the only outcome that matters is whether
    the server accepted it, and a status of 400 or above is raised as a
    failure so it reaches the action log instead of passing as a success.

    A body is sent as JSON unless the headers say otherwise, since JSON is
    what the APIs a deck key is likely to poke (Home Assistant, OBS, most
    home grown services) expect. Nothing here checks that the body parses.

    Blocks the action thread for up to the timeout. That is the caller's
    problem to bound, and the config layer caps it.
    </remarks>
    
    <exception cref="requests.RequestException">The request could not be sent,
    timed out, or came back with an error status.</exception>"""
    sent = dict(headers)
    data = None
    if body is not None:
        data = body.encode("utf-8")
        if not any(name.lower() == "content-type" for name in sent):
            sent["Content-Type"] = "application/json"
    response = requests.request(method, url, headers=sent, data=data, timeout=timeout)
    response.raise_for_status()


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
    platform, which means the config was written wrongly. An empty command
    is not an error here: it comes back empty and the runner skips it.</exception>"""
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
                 type_text: Callable[[str, int], None] = hotkeys.type_text,
                 send_request: Callable[[str, str, dict, str | None, float], None] = default_request,
                 read_token: Callable[[str], str] = read_token,
                 audio=None,
                 desktop=None,
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
        text typer, the launcher, the URL opener, the request sender, the token
        reader, the sound backend, the desktop, the sleep and the random
        source. The defaults are the real implementations, so constructing
        this with only a context gives a runner that genuinely presses keys,
        talks to the network and changes the volume.

        The sound backend and the desktop are built lazily on first use when
        not given, so a machine without pactl or xdotool starts the same and
        only the keys that need them complain, in the log, when pressed.

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
        self.type_text = type_text
        self.send_request = send_request
        self.read_token = read_token
        self._audio = audio
        self._desktop = desktop
        self.log = log

    @property
    def audio(self):
        """<summary>The sound backend, built for this machine on first use.</summary>
        <returns>A backend from the audio module.</returns>"""
        if self._audio is None:
            self._audio = audio_module.default_backend()
        return self._audio

    @property
    def desktop(self):
        """<summary>The desktop, built for this machine on first use.</summary>
        <returns>A desktop from the desktop module.</returns>"""
        if self._desktop is None:
            self._desktop = desktop_module.default_desktop()
        return self._desktop

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
        elif action.type == "text":
            # Empty text with enter still presses enter: a key that only
            # sends a return is a reasonable thing to want.
            if params["text"]:
                self.type_text(params["text"], params.get("delay_ms", 0))
            if params.get("enter"):
                self.send_hotkey("enter")
        elif action.type == "launch":
            # Nothing to run yet: the key was saved before its command was
            # typed, and doing nothing beats launching an empty string.
            command = platform_command(params)
            if command:
                self.launch(command)
        elif action.type == "url":
            self.open_url(params["url"])
        elif action.type == "request":
            headers = dict(params.get("headers", {}))
            if params.get("token_file"):
                headers["Authorization"] = self.read_token(params["token_file"])
            self.send_request(params["method"], params["url"], headers,
                              params.get("body"), params["timeout_s"])
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
        elif action.type == "volume":
            self._volume(params)
        elif action.type == "audio_output":
            self._audio_output(params)
        elif action.type == "window":
            self._window(params)
        elif action.type in POSITIONAL_ACTIONS:
            raise ValueError(f"a {action.type} action acts on its key and must be run by the controller")
        else:
            raise ValueError(f"unknown action type {action.type}")

    def _volume(self, params: dict) -> None:
        """<summary>
        Set, step or mute the sound, falling back to the media keys where
        the sound backend cannot help.
        </summary>
        <param name="params">A volume action's params: one of ``value``,
        ``delta`` or ``mute``.</param>
        <remarks>
        The fallback is for Windows without pycaw, and for any machine whose
        backend raises: a step becomes a run of media volume presses, two
        percent each as Windows counts them, and a mute becomes the media
        mute key. An absolute level has no fallback, since no key sets one,
        so that is logged with what to install. Muting "on" or "off" through
        the fallback can only toggle, which is said in the log.
        </remarks>
        """
        try:
            if "value" in params:
                self.audio.set_volume(params["value"])
            elif "delta" in params:
                self.audio.change_volume(params["delta"])
            else:
                self.audio.set_mute(params["mute"])
        except AudioError as err:
            if "value" in params:
                raise
            self.log(f"volume: {err}; using the media keys instead")
            if "delta" in params:
                delta = params["delta"]
                combo = "media_volume_up" if delta > 0 else "media_volume_down"
                for _ in range(max(1, round(abs(delta) / 2))):
                    self.send_hotkey(combo)
            else:
                if params["mute"] != "toggle":
                    self.log("volume: the media mute key can only toggle")
                self.send_hotkey("media_volume_mute")

    def _audio_output(self, params: dict) -> None:
        """<summary>
        Switch the default output to the one the key names, or the next one.
        </summary>
        <param name="params">An audio_output action's params: ``device`` or ``cycle``.</param>
        <remarks>Nothing matching is logged rather than raised, with the
        outputs that exist, so the user can see what to write.</remarks>
        <exception cref="AudioError">The outputs could not be listed or switched.</exception>"""
        outputs = self.audio.outputs()
        if params.get("cycle"):
            chosen = audio_module.pick_output(outputs, cycle=True, current=self.audio.default_output())
        else:
            chosen = audio_module.pick_output(outputs, params["device"])
        if chosen is None:
            names = ", ".join(output.name for output in outputs) or "none found"
            self.log(f"audio output: nothing matches '{params.get('device', 'cycle')}' (outputs: {names})")
            return
        self.audio.set_output(chosen.id)

    def _window(self, params: dict) -> None:
        """<summary>
        Act on the window the key names, or launch its program when there is
        no such window.
        </summary>
        <param name="params">A window action's params: ``match``,
        ``operation`` and an optional ``command``.</param>
        <remarks>The launch happens only for the focus operation. Minimising
        or closing a program that is not running is nothing to do, and
        starting it would be the opposite of what was asked.</remarks>
        <exception cref="DesktopError">The desktop could not be driven.</exception>"""
        found = desktop_module.act(params["match"], params["operation"], self.desktop)
        if found or params["operation"] != "focus":
            return
        command = params.get("command")
        if command:
            self.launch(command)
        else:
            self.log(f"window: nothing matches '{params['match']}' and no command to launch")

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
