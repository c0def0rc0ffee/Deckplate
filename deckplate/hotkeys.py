"""<summary>
Send a key combination, or type a piece of text, at whatever has focus.
</summary>
<remarks>
Combinations are written the same way on both platforms, for example
"ctrl+alt+t", "shift+f5" or "media_volume_mute". The names are normalised
here and handed to a backend: on Linux the X test extension driven directly
through python-xlib, then pynput, then xdotool; on Windows pynput. Typing
text goes through the same backends and is the one path that never parses
anything: the characters are sent as written, so a plus sign in a piece of
text is a plus sign and not a separator.

The X test backend exists because of how pynput sends a letter on X11. A
named key such as F1 or alt goes through the X test extension and looks to
every program like a real key. A key given as a character, which is every
letter and digit, is instead sent as a synthetic client event aimed at the
focused window, and a game, or anything under Wine, ignores synthetic events
entirely. So with pynput "f1" reached Star Citizen and "alt+h" never did:
alt arrived, h did not. Sending every key through X test is what the game
needs, and the fix was found on 17/10/2026 with xev, which marks the
synthetic ones.

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
import sys
import threading
import time
from typing import Callable

# The names that may only appear before the main key. A combination ending
# in one of these is rejected rather than sent, because a modifier on its own
# end has no key to modify and reads as a typing slip.
#
# The bare names ctrl, alt and shift are the left hand key, which is what both
# backends have always sent for them, so no existing config changes meaning.
# The _l and _r spellings are for programs that tell the two sides apart:
# Star Citizen binds a good deal to right alt and treats it as a different
# modifier from the left one. There is no cmd_r because pynput has no right
# hand super key on every platform and X11 names it the same as the left.
SIDED_MODIFIERS = ("ctrl_l", "ctrl_r", "alt_l", "alt_r", "shift_l", "shift_r")
MODIFIERS = ("ctrl", "alt", "shift", "cmd") + SIDED_MODIFIERS

# What people actually type, mapped to the one spelling the rest of the module
# uses. This exists so a config file written by hand is forgiving: cmd, super,
# win, windows and meta are all the same key. Add to it freely, but every value
# has to appear in NAMED_KEYS or be a single character, or the alias will be
# accepted by the parser and then fail at the backend.
ALIASES = {
    "control": "ctrl", "ctl": "ctrl",
    "option": "alt",
    # Left and right hand modifiers, in the spellings people and games use.
    # AltGr is the right alt on the keyboards this is written for, and Star
    # Citizen's own bindings screen calls that key RAlt, so both land on alt_r.
    "lalt": "alt_l", "left_alt": "alt_l", "leftalt": "alt_l",
    "ralt": "alt_r", "right_alt": "alt_r", "rightalt": "alt_r",
    "altgr": "alt_r", "alt_gr": "alt_r",
    "lctrl": "ctrl_l", "left_ctrl": "ctrl_l", "leftctrl": "ctrl_l", "lcontrol": "ctrl_l",
    "rctrl": "ctrl_r", "right_ctrl": "ctrl_r", "rightctrl": "ctrl_r", "rcontrol": "ctrl_r",
    "lshift": "shift_l", "left_shift": "shift_l", "leftshift": "shift_l",
    "rshift": "shift_r", "right_shift": "shift_r", "rightshift": "shift_r",
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
    "ctrl", "alt", "shift", "cmd", *SIDED_MODIFIERS, "enter", "esc", "space", "tab", "backspace",
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
    # xdotool's bare ctrl, alt and shift resolve to the left keysym, so the
    # _l spellings name it outright and the _r ones pick the other side.
    "ctrl_l": "Control_L", "ctrl_r": "Control_R",
    "alt_l": "Alt_L", "alt_r": "Alt_R",
    "shift_l": "Shift_L", "shift_r": "Shift_R",
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

# House names translated into X keysym numbers for the X test backend. These
# are the X11 constants themselves rather than lookups by name, because
# python-xlib's name table does not carry the XF86 media keys unless asked
# and the numbers have not changed in thirty years. A single character is not
# listed: its keysym is its Latin-1 code, or the Unicode keysym above that.
XTEST_KEYSYMS = {
    "ctrl": 0xFFE3, "ctrl_l": 0xFFE3, "ctrl_r": 0xFFE4,
    "alt": 0xFFE9, "alt_l": 0xFFE9, "alt_r": 0xFFEA,
    "shift": 0xFFE1, "shift_l": 0xFFE1, "shift_r": 0xFFE2,
    "cmd": 0xFFEB,
    "enter": 0xFF0D, "esc": 0xFF1B, "space": 0x20, "tab": 0xFF09,
    "backspace": 0xFF08, "delete": 0xFFFF,
    "up": 0xFF52, "down": 0xFF54, "left": 0xFF51, "right": 0xFF53,
    "home": 0xFF50, "end": 0xFF57, "page_up": 0xFF55, "page_down": 0xFF56,
    "insert": 0xFF63, "print_screen": 0xFF61, "caps_lock": 0xFFE5,
    "media_volume_up": 0x1008FF13, "media_volume_down": 0x1008FF11,
    "media_volume_mute": 0x1008FF12, "media_play_pause": 0x1008FF14,
    "media_next": 0x1008FF17, "media_previous": 0x1008FF16,
    "+": 0x2B,
}
XTEST_KEYSYMS.update({f"f{n}": 0xFFBE + n - 1 for n in range(1, 25)})
# Where a keysym has no key on the keymap, these are tried in turn. The one
# that matters is right alt: on a UK or US layout under X the physical right
# alt key carries ISO_Level3_Shift (AltGr) and nothing carries Alt_R, so
# asking for Alt_R finds no key at all. The physical key is what the game
# sees pressed either way, so sending its keycode is what a right alt bind
# wants.
XTEST_FALLBACK_KEYSYMS = {0xFFEA: (0xFE03, 0xFF7E)}  # Alt_R: ISO_Level3_Shift, Mode_switch
SHIFT_KEYSYM = 0xFFE1


def xtest_keysym(name: str) -> int:
    """<summary>
    The X keysym for one house key name or single character.
    </summary>
    <param name="name">A normalised name from <see cref="parse_combo"/>, or
    one character of text.</param>
    <returns>The keysym number.</returns>
    <remarks>
    A character maps the way X defines it: Latin-1 is its own keysym and
    anything beyond is the Unicode code point with the high bit set that X
    reserves for that. Whether the keymap has a key for the keysym is a
    separate question answered at send time, so this never fails for a
    character.
    </remarks>
    <exception cref="HotkeyError">A named key the table does not know, which
    means a name was added to NAMED_KEYS without a keysym here.</exception>"""
    if len(name) == 1:
        point = ord(name)
        return point if point < 0x100 else 0x01000000 | point
    try:
        return XTEST_KEYSYMS[name]
    except KeyError:
        raise HotkeyError(f"no X keysym known for '{name}'") from None


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


def xdotool_type_args(text: str, delay_ms: int = 0) -> list[str]:
    """<summary>
    Build the xdotool command line that types a piece of text.
    </summary>
    <param name="text">The text, sent exactly as written.</param>
    <param name="delay_ms">The pause between characters. Zero lets xdotool use
    its own default, which is a few milliseconds and not nothing.</param>
    <returns>The argument list to hand to subprocess, xdotool included.</returns>
    <remarks>
    The double dash before the text is not decoration: text that starts with a
    dash would otherwise be read by xdotool as an option and refused, or worse,
    obeyed. Modifiers are cleared first so a key still held on the deck does
    not turn the text into a run of shortcuts.

    Split out from the backend so the command line can be asserted in a test
    without xdotool being installed.
    </remarks>
    """
    args = ["xdotool", "type", "--clearmodifiers"]
    if delay_ms > 0:
        args += ["--delay", str(delay_ms)]
    return args + ["--", text]


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
    a mapping, as the xdotool side already has. The sided modifiers are the
    same story: pynput's Key has alt_l, alt_r, ctrl_l, ctrl_r, shift_l and
    shift_r under exactly those names, and its bare alt, ctrl and shift are
    the left hand keys, which is why the bare house names mean left too.
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

    def type(self, text: str, delay_ms: int = 0) -> None:
        """<summary>
        Type a piece of text, character by character.
        </summary>
        <param name="text">The text, sent exactly as written. A newline is
        pressed as enter, which is pynput's own behaviour.</param>
        <param name="delay_ms">A pause after each character, or zero to let
        pynput go as fast as it can.</param>
        <remarks>
        With no delay the whole string is handed to pynput in one call. With
        one, each character is typed on its own with a sleep between, because
        pynput has no pacing of its own and some programs, a game's chat box
        or a remote desktop, drop characters that arrive too fast.

        Whatever has focus gets the text. There is no way to know whether it
        went where the user meant, so a text action is only as safe as the
        moment it is pressed.
        </remarks>
        """
        if delay_ms <= 0:
            self._controller.type(text)
            return
        for index, character in enumerate(text):
            if index:
                time.sleep(delay_ms / 1000)
            self._controller.type(character)


class XTestBackend:
    """<summary>
    Sends keys through the X test extension directly, the preferred backend
    on Linux.
    </summary>
    <remarks>
    Every key, a letter as much as a modifier, becomes a fake key event
    inside the X server, so a program sees exactly what it sees from the
    keyboard: no synthetic flag, modifier state kept by the server, and a
    place in the raw input stream that Wine and games read. That is the one
    thing pynput does not do for letters, and why this class exists.

    One Display connection for the life of the daemon, guarded by a lock
    because python-xlib is not thread safe and hold threads press at the same
    time as key presses. The lock is held per key event, not per combination,
    so two tasks can interleave their keys, which matches what pynput and
    xdotool already allowed.

    Works under X11 and under XWayland, where the events reach X programs
    that have focus, which is where a Wine game lives. It cannot reach a
    native Wayland window, the same limit as the other two backends.
    </remarks>
    """

    def __init__(self, display=None, fake_input=None) -> None:
        """<summary>
        Connect to the X server and check the test extension is there.
        </summary>
        <param name="display">An open python-xlib Display, or None to open
        the default one. Tests pass a fake with the three methods used here.</param>
        <param name="fake_input">The function that fakes one key event,
        with the signature of Xlib.ext.xtest.fake_input, or None for that
        one. Tests pass a recorder.</param>
        <remarks>
        <see cref="default_backend"/> relies on this raising when there is no
        display or no extension, so it must never be tolerant: a backend that
        builds and then fails on every key would stop pynput and xdotool from
        being tried.
        </remarks>
        <exception cref="ImportError">python-xlib is not installed.</exception>
        <exception cref="HotkeyError">The server has no XTEST extension.</exception>
        <exception cref="Exception">No display to connect to.</exception>"""
        if display is None or fake_input is None:
            from Xlib import display as xdisplay  # imported here so the module loads without it
            from Xlib.ext import xtest
            if display is None:
                display = xdisplay.Display()
            if fake_input is None:
                fake_input = xtest.fake_input
            if display.query_extension("XTEST") is None:
                raise HotkeyError("the X server has no XTEST extension")
        self._display = display
        self._fake_input = fake_input
        self._lock = threading.Lock()

    def _keycode(self, name: str) -> tuple[int, bool]:
        """<summary>
        The keycode for a name, and whether shift has to be held to reach it.
        </summary>
        <param name="name">A normalised key name or a single character.</param>
        <returns>The keycode and a flag that is true when the keysym sits on
        the key's shifted level, as a capital letter or a symbol does.</returns>
        <remarks>
        A keysym with no key is tried through the fallbacks first, which is
        how right alt finds the AltGr key. The shift flag is read from the
        keymap rather than guessed from the character, so it is right for
        whatever layout is loaded: an at sign needs shift on a UK keyboard and
        not on some others.
        </remarks>
        <exception cref="HotkeyError">No key on this keymap produces the
        keysym, even through the fallbacks.</exception>"""
        keysym = xtest_keysym(name)
        for candidate in (keysym, *XTEST_FALLBACK_KEYSYMS.get(keysym, ())):
            keycode = self._display.keysym_to_keycode(candidate)
            if keycode:
                shifted = (self._display.keycode_to_keysym(keycode, 0) != candidate
                           and self._display.keycode_to_keysym(keycode, 1) == candidate)
                return keycode, shifted
        raise HotkeyError(f"no key on this keyboard layout for '{name}'")

    def _event(self, keycode: int, press: bool) -> None:
        """<summary>Fake one key event and flush it to the server.</summary>
        <param name="keycode">The keycode to press or release.</param>
        <param name="press">True for a press, false for a release.</param>
        <remarks>The sync is what makes the event leave now rather than sit
        in python-xlib's buffer until the next round trip, and the lock keeps
        two threads from interleaving inside that buffer.</remarks>"""
        with self._lock:
            self._fake_input(self._display, 2 if press else 3, keycode)  # X.KeyPress, X.KeyRelease
            self._display.sync()

    def _down(self, combo: tuple[str, ...]) -> list[int]:
        """<summary>
        Press every key in the combo and say what went down, in order.
        </summary>
        <param name="combo">Parsed key names, main key last.</param>
        <returns>The keycodes pressed, shift included where a key needed it,
        for the caller to release in reverse.</returns>
        <remarks>Every keycode is resolved before anything is pressed, so a
        name with no key raises with nothing held down.</remarks>"""
        resolved = [self._keycode(name) for name in combo]
        pressed: list[int] = []
        shift = self._keycode("shift")[0]
        try:
            for keycode, shifted in resolved:
                if shifted and shift not in pressed:
                    self._event(shift, True)
                    pressed.append(shift)
                self._event(keycode, True)
                pressed.append(keycode)
        except Exception:
            for keycode in reversed(pressed):
                self._event(keycode, False)
            raise
        return pressed

    def send(self, combo: tuple[str, ...]) -> None:
        """<summary>
        Tap the combination: everything down in order, everything up in
        reverse.
        </summary>
        <param name="combo">Parsed key names, main key last.</param>
        <remarks>
        A failure while pressing is handled inside <see cref="_down"/>, which
        releases whatever had gone down before raising, so a modifier cannot
        be left stuck in the X server, where it would apply to every keystroke
        on the machine until something else pressed it.
        </remarks>"""
        pressed = self._down(combo)
        for keycode in reversed(pressed):
            self._event(keycode, False)

    def press(self, combo: tuple[str, ...]) -> None:
        """<summary>Push every key in the combo down and leave it down.</summary>
        <param name="combo">Parsed key names, pressed in the order given.</param>
        <remarks>The caller owns the release, as with every backend. A
        character that needs shift gets shift pressed before it, and
        <see cref="release"/> lets that shift go again.</remarks>"""
        self._down(combo)

    def release(self, combo: tuple[str, ...]) -> None:
        """<summary>Let go of every key in the combo, in reverse order.</summary>
        <param name="combo">Parsed key names, released last one first.</param>
        <remarks>Releasing a key that is already up is harmless in X, which is
        what lets callers release defensively on the way out.</remarks>"""
        resolved = [self._keycode(name) for name in combo]
        shift = self._keycode("shift")[0]
        for keycode, shifted in reversed(resolved):
            self._event(keycode, False)
            if shifted:
                self._event(shift, False)

    def type(self, text: str, delay_ms: int = 0) -> None:
        """<summary>
        Type a piece of text, one real key event per character.
        </summary>
        <param name="text">The text, sent exactly as written. A newline is
        pressed as enter and a tab as tab.</param>
        <param name="delay_ms">A pause between characters, or zero for a
        short fixed one.</param>
        <remarks>
        Each character is tapped as the key that produces it on the current
        layout, with shift held for the ones that need it, so the text arrives
        in a game the same way the keys do. A character with no key on the
        layout stops the text with an error rather than typing something
        else in its place.

        The pause is never nothing, because the X server will collapse a
        press and release that arrive in the same instant for some clients.
        </remarks>
        <exception cref="HotkeyError">A character the layout cannot type.</exception>"""
        pause = max(delay_ms, 10) / 1000
        for index, character in enumerate(text):
            if index:
                time.sleep(pause)
            name = {"\n": "enter", "\t": "tab"}.get(character, character)
            self.send((name,))


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

    def type(self, text: str, delay_ms: int = 0) -> None:
        """<summary>Type a piece of text through xdotool type.</summary>
        <param name="text">The text, sent exactly as written.</param>
        <param name="delay_ms">The pause xdotool puts between characters, or
        zero for its default.</param>
        <remarks>One process for the whole string, so this is the one xdotool
        call that keeps pace with what was asked. The timeout scales with the
        text, since a long piece typed slowly is legitimately slow.</remarks>
        <exception cref="subprocess.CalledProcessError">xdotool refused it.</exception>
        <exception cref="FileNotFoundError">xdotool is not installed.</exception>"""
        budget = 5 + len(text) * max(delay_ms, 10) / 1000
        subprocess.run(xdotool_type_args(text, delay_ms), check=True, timeout=budget)


def default_backend():
    """<summary>
    Pick the best backend this machine can actually use.
    </summary>
    <returns>On Linux an X test backend where one can be built, else a pynput
    one, else an xdotool one. Elsewhere pynput.</returns>
    <remarks>
    Probed in order of preference. X test comes first on Linux because it is
    the only one of the three that a game sees letters from; see the module
    notes. The broad excepts are deliberate, because a missing display is
    signalled by something other than ImportError by both libraries and every
    case means the same thing here: try the next one.

    Only the presence of xdotool on the path is checked, not that it can talk
    to a display. A Wayland session will therefore get an xdotool backend that
    builds and then fails on every keystroke.

    The choice is announced through <see cref="announce_to"/>, because which
    backend is in use is the first question when a key reaches the desktop
    but not a game, and so is why the preferred one was passed over.
    </remarks>
    
    <exception cref="HotkeyError">None is available, with a message naming
    what to install.</exception>"""
    if sys.platform.startswith("linux"):
        try:
            backend = XTestBackend()
            _announce(f"hotkeys through {describe(backend)}")
            return backend
        except Exception as error:  # python-xlib missing, no display, or no extension
            _announce(f"X test backend unavailable ({error}); trying pynput")
    try:
        backend = PynputBackend()
        _announce(f"hotkeys through {describe(backend)}")
        return backend
    except Exception:  # pynput missing, or no display to attach to
        if shutil.which("xdotool"):
            backend = XdotoolBackend()
            _announce(f"hotkeys through {describe(backend)}")
            return backend
        raise HotkeyError("no hotkey backend: pip install pynput, or on Linux install xdotool")


def describe(backend) -> str:
    """<summary>
    The name of a backend as the log should give it.
    </summary>
    <param name="backend">Any backend object.</param>
    <returns>A short phrase: the X test extension, pynput, xdotool, or the
    class name for anything else, such as a test's recorder.</returns>
    """
    names = {XTestBackend: "the X test extension", PynputBackend: "pynput", XdotoolBackend: "xdotool"}
    return names.get(type(backend), type(backend).__name__)


# Where the backend choice is reported. Nothing by default, because this
# module has no log of its own and importing it must print nothing; the
# daemon hands its log in through announce_to so the line lands in run.log
# with the rest, on the same clock.
_announce: Callable[[str], None] = lambda text: None


def announce_to(log: Callable[[str], None]) -> None:
    """<summary>
    Send the backend choice, when it is made, to this log callback.
    </summary>
    <param name="log">A function taking one line of text, such as the
    daemon's say.</param>
    <remarks>
    The choice is made once, at the first key sent, so a callback registered
    after that hears nothing. The action runner registers its log when it is
    built, which is before any key can be pressed.
    </remarks>
    """
    global _announce
    _announce = log


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


def type_text(text: str, delay_ms: int = 0, backend=None) -> None:
    """<summary>
    Type a piece of text at whatever has focus.
    </summary>
    <param name="text">The text, sent exactly as written. Nothing in it is
    parsed as a key name.</param>
    <param name="delay_ms">A pause between characters, or zero for none.</param>
    <param name="backend">A backend for this call only, or None for the shared one.</param>
    <remarks>
    The one entry point here that does not go through <see cref="parse_combo"/>,
    which is the point: text is data, and a piece of text that happens to read
    "ctrl+c" is typed as those six characters. Blocks until the last character
    has been handed over, which with a delay is the length of the text times
    the delay.
    </remarks>
    
    <exception cref="HotkeyError">There is no backend.</exception>"""
    _resolve_backend(backend).type(text, delay_ms)
