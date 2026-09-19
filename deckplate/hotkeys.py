"""<summary>
Send a key combination to whatever has focus.
</summary>
<remarks>
Combinations are written the same way on both platforms, for example
"ctrl+alt+t", "shift+f5" or "media_volume_mute". The names are normalised
here and handed to a backend: pynput on Windows and on X11 Linux, with
xdotool as the Linux fallback when pynput is not installed.

There is no way to ask where a keystroke went. Everything here is fire and
forget into whatever window has focus at that instant, so a key pressed while
the configuration page is focused types into the page.

Wayland is the known gap. Neither backend can inject into a Wayland session,
so a Linux machine has to be on X11 for any of this to work. That is a
limitation to state plainly rather than to work around.

The module keeps one lazily built backend in a module level variable, so the
first call is what probes for pynput and xdotool. Every public call takes an
optional backend argument which bypasses that entirely, which is how the tests
run without a display.
</remarks>
"""

from __future__ import annotations

import shutil
import subprocess

# The four names that may only appear before the main key. A combination
# ending in one of these is rejected rather than sent, because a modifier on
# its own end has no key to modify and reads as a typing slip.
MODIFIERS = ("ctrl", "alt", "shift", "cmd")

# What people actually type, mapped to the one spelling the rest of the module
# uses. This exists so a config file written by hand is forgiving: cmd, super,
# win, windows and meta are all the same key. Add to it freely, but every value
# has to appear in NAMED_KEYS or be a single character, or the alias will be
# accepted by the parser and then fail at the backend.
ALIASES = {
    "control": "ctrl", "ctl": "ctrl",
    "option": "alt",
    "super": "cmd", "win": "cmd", "windows": "cmd", "meta": "cmd",
    "return": "enter",
    "escape": "esc",
    "del": "delete",
    "pageup": "page_up", "pgup": "page_up",
    "pagedown": "page_down", "pgdn": "page_down",
    "printscreen": "print_screen", "prtsc": "print_screen",
    "capslock": "caps_lock",
    "plus": "+",
    "volumeup": "media_volume_up", "volup": "media_volume_up",
    "volumedown": "media_volume_down", "voldown": "media_volume_down",
    "mute": "media_volume_mute", "volumemute": "media_volume_mute",
    "playpause": "media_play_pause", "play": "media_play_pause",
    "nexttrack": "media_next", "next": "media_next",
    "prevtrack": "media_previous", "previous": "media_previous",
}

# Every key whose name is longer than one character. The parser treats a single
# character as a literal and anything else as a lookup here, so a key missing
# from this set is reported as unknown even when the backend would have taken
# it. These names are the pynput spellings, which is why the xdotool backend
# needs a translation table and pynput does not.
NAMED_KEYS = {
    "ctrl", "alt", "shift", "cmd", "enter", "esc", "space", "tab", "backspace",
    "delete", "up", "down", "left", "right", "home", "end", "page_up",
    "page_down", "insert", "print_screen", "caps_lock", "media_volume_up",
    "media_volume_down", "media_volume_mute", "media_play_pause", "media_next",
    "media_previous",
} | {f"f{n}" for n in range(1, 25)}

# House names translated into X keysym names for the xdotool backend. Anything
# not listed is passed through unchanged, which is right for plain characters
# and wrong for a named key that is added to NAMED_KEYS and forgotten here: it
# would reach xdotool as a name X does not know. Note page_up and page_down
# become Prior and Next, and a literal plus becomes the word plus, because a
# bare + is the separator in xdotool's own syntax.
XDOTOOL_NAMES = {
    "ctrl": "ctrl", "alt": "alt", "shift": "shift", "cmd": "super",
    "enter": "Return", "esc": "Escape", "space": "space", "tab": "Tab",
    "backspace": "BackSpace", "delete": "Delete", "up": "Up", "down": "Down",
    "left": "Left", "right": "Right", "home": "Home", "end": "End",
    "page_up": "Prior", "page_down": "Next", "insert": "Insert",
    "print_screen": "Print", "caps_lock": "Caps_Lock",
    "media_volume_up": "XF86AudioRaiseVolume",
    "media_volume_down": "XF86AudioLowerVolume",
    "media_volume_mute": "XF86AudioMute",
    "media_play_pause": "XF86AudioPlay",
    "media_next": "XF86AudioNext",
    "media_previous": "XF86AudioPrev",
    "+": "plus",
}
XDOTOOL_NAMES.update({f"f{n}": f"F{n}" for n in range(1, 25)})


class HotkeyError(ValueError):
    """<summary>
    A combination cannot be parsed, or there is no backend to send it with.
    </summary>
    <remarks>
    A subclass of ValueError so a caller that only cares about bad input can
    catch either. It is raised for a user's typing mistake and for a missing
    backend alike, so the message is the only thing that tells the two apart:
    both end up logged against the key that was pressed, which is what the
    reader needs to see.
    </remarks>
    """

    pass


def parse_combo(text: str) -> tuple[str, ...]:
    """<summary>
    Normalise a written combination into the names the backends take.
    </summary>
    <param name="text">A combination such as "Ctrl + Alt + T". Spaces are
    ignored, case is ignored, and aliases are resolved.</param>
    <returns>The key names in the order written, modifiers first. Single
    characters are left exactly as they are.</returns>
    <remarks>
    'Ctrl + Alt + T' becomes ('ctrl', 'alt', 't'). The last name is treated as
    the main key by every backend and the ones before it as modifiers, so the
    order matters even though the set does not.

    The literal plus is the awkward case. Splitting "ctrl++" on the separator
    gives an empty part followed by another empty part, and that pair is what
    means a plus was meant. A single empty part is a typing mistake and raises.

    Nothing here checks that the key exists on the keyboard in front of you,
    only that the name is one the module knows. A valid name for a key the
    layout does not have fails later, at the backend.
    </remarks>
    
    <exception cref="HotkeyError">An empty part, an unknown key name, nothing
    at all, or a combination ending in a modifier.</exception>"""
    parts = [part.strip().lower() for part in text.replace(" ", "").split("+")]
    # "ctrl++" splits into ['ctrl', '', '']: an empty part after a '+' means a literal plus.
    names: list[str] = []
    skip = False
    for index, part in enumerate(parts):
        if skip:
            skip = False
            continue
        if part == "" and index + 1 < len(parts) and parts[index + 1] == "":
            names.append("+")
            skip = True
            continue
        if part == "":
            raise HotkeyError(f"empty key in '{text}'")
        part = ALIASES.get(part, part)
        if len(part) != 1 and part not in NAMED_KEYS:
            raise HotkeyError(f"unknown key '{part}' in '{text}'")
        names.append(part)
    if not names:
        raise HotkeyError("no keys given")
    if names[-1] in MODIFIERS and len(names) > 1:
        raise HotkeyError(f"'{text}' ends with a modifier; put the main key last")
    return tuple(names)


def xdotool_args(combo: tuple[str, ...], verb: str = "key") -> list[str]:
    """<summary>
    Build the xdotool command line for a combination.
    </summary>
    <param name="combo">Parsed key names from <see cref="parse_combo"/>.</param>
    <param name="verb">key to tap, keydown to press and hold, keyup to let go.</param>
    <returns>The argument list to hand to subprocess, xdotool included.</returns>
    <remarks>
    Only the tap gets ``--clearmodifiers``. That flag lifts any modifier the
    user is physically holding, sends the combination and puts them back, which
    is what stops a hand resting on shift from corrupting a hotkey. It is
    deliberately left off keydown and keyup, because there the whole point is
    to change what is held and restoring it afterwards would undo the call.

    Split out from the backend so the command line can be asserted in a test
    without xdotool being installed.
    </remarks>
    """
    names = "+".join(XDOTOOL_NAMES.get(n, n) for n in combo)
    if verb == "key":
        return ["xdotool", "key", "--clearmodifiers", names]
    return ["xdotool", verb, names]


def pynput_attribute(name: str) -> str | None:
    """<summary>
    The pynput Key attribute for a named key, or None for a plain character.
    </summary>
    <param name="name">One normalised key name.</param>
    <returns>The attribute name to look up on pynput's Key, or None when the
    caller should build a character key instead.</returns>
    <remarks>
    The house names were chosen to match pynput's attribute names exactly, so
    this is a pass through rather than a table. That is worth knowing before
    adding a key whose pynput spelling differs: this function would then need
    a mapping, as the xdotool side already has.
    </remarks>
    """
    if len(name) == 1:
        return None
    return name


class PynputBackend:
    """<summary>
    Sends keys through pynput, the preferred backend on both platforms.
    </summary>
    <remarks>
    The only backend on Windows and the better one on X11 Linux, because it
    holds one connection for the life of the daemon instead of starting a
    process per keystroke.

    Constructing it is the test for whether it can be used: pynput is imported
    and a controller built here, so a missing library or a missing display
    raises now rather than on the first key press.

    The controller is not thread safe in any documented way, and several hold
    threads may be pressing at once. In practice each thread only touches its
    own keys, so they do not collide, but a combination shared by two tasks can
    be released by one while the other still believes it is down.
    </remarks>
    """

    def __init__(self) -> None:
        """<summary>
        Connect to pynput, raising if it is not usable here.
        </summary>
        <remarks>
        <see cref="default_backend"/> relies on this raising, so never make the
        constructor tolerant: a backend that builds and then fails on every key
        press would stop xdotool from ever being tried.
        </remarks>
        
        <exception cref="ImportError">pynput is not installed.</exception>
        <exception cref="Exception">pynput is installed but cannot attach, which
        is what a headless machine or a Wayland session looks like.</exception>"""
        from pynput import keyboard  # imported here so the module loads without it

        self._keyboard = keyboard
        self._controller = keyboard.Controller()

    def _key(self, name: str):
        """
        <summary>
        The pynput key object for a name: a named Key when pynput has one, otherwise a KeyCode for the character.
        </summary>
        <param name="name">A key name from a parsed combination.</param>
        <returns>A pynput Key or KeyCode.</returns>
        """
        attribute = pynput_attribute(name)
        if attribute is None:
            return self._keyboard.KeyCode.from_char(name)
        return getattr(self._keyboard.Key, attribute)

    def send(self, combo: tuple[str, ...]) -> None:
        """<summary>
        Tap the combination: modifiers down, main key down and up, modifiers up.
        </summary>
        <param name="combo">Parsed key names, main key last.</param>
        <remarks>
        Modifiers are released in reverse order inside a finally block, so a
        main key that fails to press cannot leave ctrl or alt stuck down. That
        is the single most important line in this class: a stuck modifier makes
        the whole machine unusable and gives no clue why.

        A one key combination has no modifiers and is simply pressed and
        released.
        </remarks>
        """
        keys = [self._key(name) for name in combo]
        modifiers, main = keys[:-1], keys[-1]
        for key in modifiers:
            self._controller.press(key)
        try:
            self._controller.press(main)
            self._controller.release(main)
        finally:
            for key in reversed(modifiers):
                self._controller.release(key)

    def press(self, combo: tuple[str, ...]) -> None:
        """<summary>
        Push every key in the combo down and leave it down.
        </summary>
        <param name="combo">Parsed key names, pressed in the order given.</param>
        <remarks>
        Nothing here will ever release them. The caller owns that, and a caller
        that forgets leaves the keyboard held down with no visible cause. Every
        caller in this project pairs this with a release in a finally block.

        Unlike <see cref="send"/> there is no cleanup if a key part way through
        raises: the ones already down stay down, because releasing them would
        fight a hold that another task may legitimately be keeping.
        </remarks>
        """
        for name in combo:
            self._controller.press(self._key(name))

    def release(self, combo: tuple[str, ...]) -> None:
        """<summary>
        Let go of every key in the combo, in reverse order.
        </summary>
        <param name="combo">Parsed key names, released last one first.</param>
        <remarks>
        Reverse order matches how they went down, so a modifier is never let go
        while the key it modifies is still held. Releasing a key that is
        already up is harmless, which is what lets callers release
        defensively on the way out.
        </remarks>
        """
        for name in reversed(combo):
            self._controller.release(self._key(name))


class XdotoolBackend:
    """<summary>
    The Linux fallback: one xdotool process per keystroke.
    </summary>
    <remarks>
    Used only when pynput cannot be built. It works on X11 and not on Wayland,
    the same limit pynput has.

    A process per keystroke is slow enough to matter. A chord or a fast repeat
    through this backend will not keep the pace the configuration asked for,
    and the gap is the process start, not the timings.

    Every call uses ``check=True``, so a failure raises and reaches the action
    runner's log rather than passing silently, and a five second timeout keeps
    a wedged xdotool from holding a hold thread forever.
    </remarks>
    """

    def send(self, combo: tuple[str, ...]) -> None:
        """<summary>Tap the combination through xdotool key.</summary>
        <param name="combo">Parsed key names.</param>
        <exception cref="subprocess.CalledProcessError">xdotool refused it.</exception>
        <exception cref="FileNotFoundError">xdotool is not installed.</exception>"""
        subprocess.run(xdotool_args(combo), check=True, timeout=5)

    def press(self, combo: tuple[str, ...]) -> None:
        """<summary>Push the combination down and leave it down.</summary>
        <param name="combo">Parsed key names.</param>
        <remarks>The keys stay down in the X server even if this process dies,
        so a crash here really does leave the keyboard held.</remarks>"""
        subprocess.run(xdotool_args(combo, "keydown"), check=True, timeout=5)

    def release(self, combo: tuple[str, ...]) -> None:
        """<summary>Let the combination go.</summary>
        <param name="combo">Parsed key names.</param>
        <remarks>Safe to call for keys that are already up.</remarks>"""
        subprocess.run(xdotool_args(combo, "keyup"), check=True, timeout=5)


def default_backend():
    """<summary>
    Pick the best backend this machine can actually use.
    </summary>
    <returns>A pynput backend where one can be built, otherwise an xdotool one.</returns>
    <remarks>
    Probed in order of preference, not of platform: pynput is tried on Linux
    too and usually wins there. The broad except is deliberate, because pynput
    signals a missing display by raising something other than ImportError and
    both cases mean the same thing here.

    Only the presence of xdotool on the path is checked, not that it can talk
    to a display. A Wayland session will therefore get an xdotool backend that
    builds and then fails on every keystroke.
    </remarks>
    
    <exception cref="HotkeyError">Neither is available, with a message naming
    what to install.</exception>"""
    try:
        return PynputBackend()
    except Exception:  # pynput missing, or no display to attach to
        if shutil.which("xdotool"):
            return XdotoolBackend()
        raise HotkeyError("no hotkey backend: pip install pynput, or on Linux install xdotool")


# The one backend shared by every call that does not bring its own. Built on
# first use rather than at import, so importing this module on a machine with
# no display costs nothing and cannot fail. It is never rebuilt: a backend that
# dies with its display stays broken until the daemon is restarted.
_backend = None


def _resolve_backend(backend):
    """<summary>
    Use the caller's backend, or build and remember the shared one.
    </summary>
    <param name="backend">A backend to use just for this call, or None.</param>
    <returns>The backend to send with.</returns>
    <remarks>
    A passed in backend is never cached, so a test cannot leave its fake behind
    for the next test. There is no lock: two threads racing on the first call
    would both build one and the second would win, which wastes a connection
    and breaks nothing.
    </remarks>
    
    <exception cref="HotkeyError">None was passed and none could be built.</exception>"""
    global _backend
    if backend is not None:
        return backend
    if _backend is None:
        _backend = default_backend()
    return _backend


def send(text: str, backend=None) -> None:
    """<summary>
    Tap a written combination at whatever has focus.
    </summary>
    <param name="text">A combination as written in the config file.</param>
    <param name="backend">A backend for this call only, or None for the shared one.</param>
    <remarks>
    The main entry point for the whole module and the default the action runner
    is built with. It blocks until the keystroke has been handed over, which on
    the xdotool backend means waiting for a process to run.
    </remarks>
    
    <exception cref="HotkeyError">The text will not parse, or there is no backend.</exception>"""
    _resolve_backend(backend).send(parse_combo(text))


def press(text: str, backend=None) -> None:
    """<summary>
    Hold the keys down until release() is called with the same text.
    </summary>
    <param name="text">A combination as written in the config file.</param>
    <param name="backend">A backend for this call only, or None for the shared one.</param>
    <remarks>
    Nothing tracks what is held, so pressing the same combination twice and
    releasing it once leaves it down on the pynput backend. Pair every call
    with a release in a finally block, which is what everything in this project
    does.
    </remarks>
    
    <exception cref="HotkeyError">The text will not parse, or there is no backend.</exception>"""
    _resolve_backend(backend).press(parse_combo(text))


def release(text: str, backend=None) -> None:
    """<summary>
    Let go of the keys a matching press() put down.
    </summary>
    <param name="text">The same combination text that was pressed.</param>
    <param name="backend">A backend for this call only, or None for the shared one.</param>
    <remarks>
    Releasing something that was never pressed does nothing, so this is safe to
    call defensively on a path where it is not certain a press happened.
    </remarks>
    
    <exception cref="HotkeyError">The text will not parse, or there is no backend.</exception>"""
    _resolve_backend(backend).release(parse_combo(text))
