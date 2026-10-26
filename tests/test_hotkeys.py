"""<summary>
Parsing key combinations and handing them to a keystroke backend.
</summary>
<remarks>
Nothing in this file may type into the real desktop. Every test either works on
pure text or passes a recording backend in, because a stray keystroke from a
test run goes to whatever window happens to have focus.

What is being protected is that a combination written in the configuration file
means one thing, spelled consistently, on both backends. The failure this
guards against is not an exception: it is the wrong keystroke arriving in
someone else's application.
</remarks>
"""

import pytest

from deckplate import hotkeys


@pytest.mark.parametrize("text, expected", [
    ("ctrl+alt+t", ("ctrl", "alt", "t")),
    ("Ctrl + Shift + F5", ("ctrl", "shift", "f5")),
    ("control+return", ("ctrl", "enter")),
    ("super+l", ("cmd", "l")),
    ("win+e", ("cmd", "e")),
    ("media_volume_mute", ("media_volume_mute",)),
    ("mute", ("media_volume_mute",)),
    ("playpause", ("media_play_pause",)),
    ("ctrl+plus", ("ctrl", "+")),
    ("ctrl++", ("ctrl", "+")),
    ("a", ("a",)),
    ("ctrl", ("ctrl",)),
    ("+", ("+",)),
    ("alt_r+x", ("alt_r", "x")),
    ("ralt+x", ("alt_r", "x")),
    ("AltGr + F1", ("alt_r", "f1")),
    ("lalt+x", ("alt_l", "x")),
    ("rctrl+rshift+x", ("ctrl_r", "shift_r", "x")),
    ("right_shift+x", ("shift_r", "x")),
    ("alt_r", ("alt_r",)),
])
def test_parse_combo(text, expected):
    """<summary>
    The spellings a configuration file is allowed to use, and what each one means.
    </summary>
    <remarks>
    Case and spaces are insignificant, control is an alias for ctrl, and both super
    and win normalise to cmd so one configuration file works on both platforms.
    Media keys can be written the short way or the long way.

    The plus sign is the trap: it is both the separator and a real key, so
    ``ctrl+plus`` and ``ctrl++`` have to mean the same thing and a bare ``+`` has
    to survive on its own.

    The sided modifiers are their own spelling and never fold into the bare
    name: ``ralt+x`` must stay ``alt_r``, because a game that binds right alt
    would otherwise be sent the left one and never see the press. The bare
    ``alt`` stays ``alt`` for the same reason in reverse: it has always been the
    left key and configs written against that must not move.
    </remarks>
    """
    assert hotkeys.parse_combo(text) == expected


@pytest.mark.parametrize("text", ["", "ctrl+", "ctrl+banana", "t+ctrl", "x+alt_r", "x+rshift", "alt_middle+x"])
def test_parse_combo_rejects(text):
    """<summary>
    Empty, unfinished and impossible combinations raise rather than being guessed
    at.
    </summary>
    <remarks>
    The last case is the important one: a modifier written after the main key is
    rejected, not reordered. Guessing what someone meant here would mean sending a
    keystroke nobody asked for into whatever is in focus, so a bad line in the
    configuration file is better reported than repaired.
    </remarks>
    """
    with pytest.raises(hotkeys.HotkeyError):
        hotkeys.parse_combo(text)


def test_xdotool_args():
    """<summary>
    A parsed combination becomes an xdotool command line with X11's own spellings.
    </summary>
    <remarks>
    X11 names are not the names people write: page up is Prior, function keys are
    capitalised, and media keys are XF86 names. The modifiers on the keyboard at
    the time are cleared, otherwise a key held on the deck while a hotkey fires
    changes what the hotkey does.
    </remarks>
    """
    assert hotkeys.xdotool_args(("ctrl", "alt", "t")) == ["xdotool", "key", "--clearmodifiers", "ctrl+alt+t"]
    assert hotkeys.xdotool_args(("cmd", "page_up"))[-1] == "super+Prior"
    assert hotkeys.xdotool_args(("media_volume_mute",))[-1] == "XF86AudioMute"
    assert hotkeys.xdotool_args(("shift", "f12"))[-1] == "shift+F12"
    # The bare modifiers keep xdotool's own left hand spellings; the sided
    # ones name the keysym, so right alt really is Alt_R and not alt.
    assert hotkeys.xdotool_args(("alt", "x"))[-1] == "alt+x"
    assert hotkeys.xdotool_args(("alt_r", "x"))[-1] == "Alt_R+x"
    assert hotkeys.xdotool_args(("alt_l", "x"))[-1] == "Alt_L+x"
    assert hotkeys.xdotool_args(("ctrl_r", "shift_r", "x"))[-1] == "Control_R+Shift_R+x"
    assert hotkeys.xdotool_args(("ctrl_l", "shift_l", "x"))[-1] == "Control_L+Shift_L+x"


def test_pynput_attribute():
    """<summary>
    Which names are special keys on the pynput backend and which are ordinary
    characters.
    </summary>
    <remarks>
    None means the name is a plain character to be sent as a character code.
    Getting this backwards sends the literal text of a key name instead of pressing
    the key, so a volume control would type its own name into the focused window.
    </remarks>
    """
    assert hotkeys.pynput_attribute("t") is None
    assert hotkeys.pynput_attribute("ctrl") == "ctrl"
    assert hotkeys.pynput_attribute("media_volume_up") == "media_volume_up"
    # The sided names are pynput's own attribute names, and the bare alt stays
    # pynput's bare alt, which is the left key.
    for name in hotkeys.SIDED_MODIFIERS:
        assert hotkeys.pynput_attribute(name) == name
    assert hotkeys.pynput_attribute("alt") == "alt"


def test_every_alias_resolves_to_a_known_key():
    """<summary>
    Every alias points at a single character or a name in NAMED_KEYS, and every
    sided modifier has an xdotool keysym.
    </summary>
    <remarks>
    The alias table is the forgiving layer and NAMED_KEYS the strict one. An
    alias whose target is missing from NAMED_KEYS passes the parser and then
    fails at the backend, on a key press, hours after the config was written.
    A sided modifier with no xdotool entry would reach xdotool spelled
    ``alt_r``, which X does not know. Both are the kind of slip that only
    shows up on the one machine that uses the spelling.
    </remarks>
    """
    for alias, target in hotkeys.ALIASES.items():
        assert len(target) == 1 or target in hotkeys.NAMED_KEYS, alias
    for name in hotkeys.SIDED_MODIFIERS:
        assert name in hotkeys.NAMED_KEYS
        assert name in hotkeys.MODIFIERS
        assert name in hotkeys.XDOTOOL_NAMES


class RecordingBackend:
    """
    <summary>
    A backend that records what it is asked to send.
    </summary>
    """
    def __init__(self):
        """
        <summary>
        Start with nothing sent.
        </summary>
        """
        self.sent = []

    def send(self, combo):
        """
        <summary>
        Record the combination.
        </summary>
        <param name="combo">The combination.</param>
        """
        self.sent.append(combo)


def test_send_uses_given_backend():
    """<summary>
    Sending parses the text and hands the result to the backend it was given.
    </summary>
    <remarks>
    The injectable backend is the whole reason this can be tested at all. If the
    parameter were dropped in favour of picking a backend internally, this test
    would start typing into the desktop of whoever ran the suite.
    </remarks>
    """
    backend = RecordingBackend()
    hotkeys.send("ctrl+shift+esc", backend=backend)
    assert backend.sent == [("ctrl", "shift", "esc")]


def test_xdotool_keydown_and_keyup():
    """<summary>
    Holding and releasing a key are separate xdotool calls, and neither clears the
    modifiers.
    </summary>
    <remarks>
    The missing ``--clearmodifiers`` is deliberate and easy to add back by mistake
    while tidying. Clearing modifiers between the down and the up would release the
    very modifier that is being deliberately held, so a key held on the deck to
    keep shift down would drop it immediately.
    </remarks>
    """
    assert hotkeys.xdotool_args(("shift", "w"), "keydown") == ["xdotool", "keydown", "shift+w"]
    assert hotkeys.xdotool_args(("w",), "keyup") == ["xdotool", "keyup", "w"]


def test_press_and_release_use_given_backend():
    """<summary>
    Press and release each parse their text and reach the backend as separate
    calls, in order.
    </summary>
    <remarks>
    This is what a held deck key is built on: the down goes out when the key is
    pressed and the up only when it is let go. If the pair were ever collapsed into
    one send, holding a key on the deck would tap instead of hold, and a game
    expecting a held key would see nothing.
    </remarks>
    """
    class Recording:
        """
        <summary>
        Records press and release calls in order.
        </summary>
        """
        def __init__(self):
            """
            <summary>
            Start with an empty call log.
            </summary>
            """
            self.calls = []

        def press(self, combo):
            """
            <summary>
            Record a press.
            </summary>
            <param name="combo">The combination.</param>
            """
            self.calls.append(("press", combo))

        def release(self, combo):
            """
            <summary>
            Record a release.
            </summary>
            <param name="combo">The combination.</param>
            """
            self.calls.append(("release", combo))

    backend = Recording()
    hotkeys.press("shift+w", backend=backend)
    hotkeys.release("shift+w", backend=backend)
    assert backend.calls == [("press", ("shift", "w")), ("release", ("shift", "w"))]


def test_pynput_backend_press_order():
    """<summary>
    Modifiers held, main key tapped, modifiers released in reverse.
    </summary>
    <remarks>
    The reverse order on release is what stops a modifier being left stuck down.
    A stuck modifier is not a small fault: every later keystroke on the machine
    arrives with it applied, and nothing in the daemon will notice or clear it.

    The fake keyboard is built by hand rather than imported, so the test runs on a
    machine with no pynput and no display, and so that nothing real is pressed.
    </remarks>
    """
    class FakeKeyboard:
        """<summary>
        A stand in for the pynput module, shaped exactly like the part of it
        the backend touches.
        </summary>
        <remarks>
        The nesting is the contract, not decoration: the backend reaches for
        ``Key.<name>`` for a modifier and ``KeyCode.from_char`` for anything
        else, so this has to mirror that shape or the test passes for the
        wrong reason. Keys are returned as marker strings rather than real
        objects so the assertions can compare them directly.

        If pynput ever moves those names, this fake keeps the test green
        while the real backend breaks. That is the known limit of building
        the double by hand, and it is accepted here so the suite runs with no
        pynput installed and no display attached.
        </remarks>
        """

        class Key:
            """
            <summary>
            The named keys the test needs, as marker strings.
            </summary>
            """
            ctrl = "K_ctrl"
            alt = "K_alt"
            enter = "K_enter"

        class KeyCode:
            """
            <summary>
            Stand-in for pynput's KeyCode.
            </summary>
            """
            @staticmethod
            def from_char(c):
                """
                <summary>
                A marker string for the character.
                </summary>
                <param name="c">One character.</param>
                <returns>A string.</returns>
                """
                return f"C_{c}"

        class Controller:
            """
            <summary>
            Records presses and releases.
            </summary>
            """
            def __init__(self):
                """
                <summary>
                Start with an empty log.
                </summary>
                """
                self.log = []

            def press(self, key):
                """
                <summary>
                Record a press.
                </summary>
                <param name="key">The key object.</param>
                """
                self.log.append(("press", key))

            def release(self, key):
                """
                <summary>
                Record a release.
                </summary>
                <param name="key">The key object.</param>
                """
                self.log.append(("release", key))

    backend = hotkeys.PynputBackend.__new__(hotkeys.PynputBackend)
    backend._keyboard = FakeKeyboard
    backend._controller = FakeKeyboard.Controller()
    backend.send(("ctrl", "alt", "t"))
    assert backend._controller.log == [
        ("press", "K_ctrl"), ("press", "K_alt"),
        ("press", "C_t"), ("release", "C_t"),
        ("release", "K_alt"), ("release", "K_ctrl"),
    ]
    backend._controller.log.clear()
    backend.press(("ctrl", "w"))
    backend.release(("ctrl", "w"))
    assert backend._controller.log == [
        ("press", "K_ctrl"), ("press", "C_w"), ("release", "C_w"), ("release", "K_ctrl")]


def test_type_text_uses_given_backend_and_never_parses():
    """<summary>
    Typing hands the text to the backend exactly as written, with the delay,
    and does not run it through the combination parser.
    </summary>
    <remarks>
    The text "ctrl+c" is the whole point of the case: parsed, it would be a
    copy shortcut, and typed it is six characters. A text action carrying a
    piece of text that happens to look like a key name must type it, because
    text is data and the user chose the type. The backend seam is what keeps
    this from typing into the desktop of whoever runs the suite.
    </remarks>
    """
    class Recording:
        """<summary>A backend that records what it was asked to type.</summary>"""

        def __init__(self):
            """<summary>Start with nothing typed.</summary>"""
            self.typed = []

        def type(self, text, delay_ms=0):
            """<summary>Record the text and the delay.</summary>"""
            self.typed.append((text, delay_ms))

    backend = Recording()
    hotkeys.type_text("ctrl+c", backend=backend)
    hotkeys.type_text("hello\nworld", delay_ms=25, backend=backend)
    assert backend.typed == [("ctrl+c", 0), ("hello\nworld", 25)]


def test_xdotool_type_args():
    """<summary>
    The xdotool line for text clears modifiers, carries the delay only when
    there is one, and puts the text after a double dash.
    </summary>
    <remarks>
    The double dash is the line that matters. Without it a piece of text
    starting with a dash is read by xdotool as an option, so "--help" would
    print xdotool's usage instead of typing it, and anything else starting
    with a dash would be refused. The delay is left out at zero so xdotool
    keeps its own small default rather than being told to type with none.
    </remarks>
    """
    assert hotkeys.xdotool_type_args("--help") == ["xdotool", "type", "--clearmodifiers", "--", "--help"]
    assert hotkeys.xdotool_type_args("hi", 40) == ["xdotool", "type", "--clearmodifiers", "--delay", "40", "--", "hi"]


class FakeDisplay:
    """<summary>
    A stand in for a python-xlib Display with a small fixed keymap.
    </summary>
    <remarks>
    The keymap is the part of a UK layout the tests lean on: h on keycode 43
    with H on its shifted level, 1 and ! sharing keycode 10, left alt on 64,
    shift on 50, and the physical right alt carrying ISO_Level3_Shift on 108
    with nothing at all carrying Alt_R, which is what a real X server under
    XWayland reported on 17/10/2026. Events are recorded as (press, keycode)
    so the order a combination went down and came up can be asserted.
    </remarks>
    """

    KEYMAP = {
        43: (0x68, 0x48), 10: (0x31, 0x21), 64: (0xFFE9, 0), 50: (0xFFE1, 0),
        37: (0xFFE3, 0), 108: (0xFE03, 0), 67: (0xFFBE, 0xFFBE), 36: (0xFF0D, 0),
    }

    def __init__(self):
        """<summary>Start with nothing sent and nothing synced.</summary>"""
        self.events = []
        self.syncs = 0

    def keysym_to_keycode(self, keysym):
        """<summary>The first keycode carrying the keysym on any level, or 0.</summary>"""
        for keycode, levels in self.KEYMAP.items():
            if keysym in levels:
                return keycode
        return 0

    def keycode_to_keysym(self, keycode, level):
        """<summary>The keysym on one level of a keycode, or 0.</summary>"""
        return self.KEYMAP.get(keycode, (0, 0))[level]

    def sync(self):
        """<summary>Count the flushes.</summary>"""
        self.syncs += 1

    def fake_input(self, display, event_type, keycode):
        """<summary>Record one faked key event in the shape xtest.fake_input takes.</summary>"""
        assert display is self
        self.events.append(("down" if event_type == 2 else "up", keycode))


def xtest_backend():
    """<summary>An X test backend wired to a fresh FakeDisplay, and the display.</summary>"""
    display = FakeDisplay()
    return hotkeys.XTestBackend(display=display, fake_input=display.fake_input), display


def test_xtest_keysym_table_covers_every_named_key():
    """<summary>
    Every name the parser accepts has a keysym, and characters map the way X
    defines them.
    </summary>
    <remarks>
    A name in NAMED_KEYS without a keysym here would parse, be accepted at
    config load, and fail on the key press. The character rule is X's own:
    Latin-1 is its own keysym and anything beyond carries the Unicode flag.
    </remarks>
    """
    for name in hotkeys.NAMED_KEYS:
        assert isinstance(hotkeys.xtest_keysym(name), int), name
    assert hotkeys.xtest_keysym("h") == 0x68
    assert hotkeys.xtest_keysym("+") == 0x2B
    assert hotkeys.xtest_keysym("é") == 0xE9
    assert hotkeys.xtest_keysym("€") == 0x01000000 | 0x20AC
    assert hotkeys.xtest_keysym("f12") == 0xFFBE + 11
    with pytest.raises(hotkeys.HotkeyError):
        hotkeys.xtest_keysym("banana")


def test_xtest_send_is_all_real_key_events_in_order():
    """<summary>
    alt+h goes down as alt then h and comes up as h then alt, every one a
    faked key event flushed to the server.
    </summary>
    <remarks>
    This is the whole reason the backend exists. With pynput the h in alt+h
    was a synthetic client event that a game ignores, so the modifier arrived
    and the letter did not. Here there is no other path: four events through
    fake_input, each followed by a sync so it leaves at once.
    </remarks>
    """
    backend, display = xtest_backend()
    hotkeys.send("alt+h", backend=backend)
    assert display.events == [("down", 64), ("down", 43), ("up", 43), ("up", 64)]
    assert display.syncs == 4


def test_xtest_shifted_character_holds_shift_from_the_keymap():
    """<summary>
    A symbol on a shifted level gets shift pressed before it and released
    after, read from the keymap rather than guessed.
    </summary>
    <remarks>
    The keymap is the authority because the same symbol needs shift on one
    layout and not on another. A letter must not pick up a shift, or every
    hotkey would arrive capitalised. Capitals cannot appear in a combination
    at all, since the parser lowercases it, so the shifted cases here are
    symbols; capitals reach this backend only through typed text.
    </remarks>
    """
    backend, display = xtest_backend()
    hotkeys.send("!", backend=backend)
    assert display.events == [("down", 50), ("down", 10), ("up", 10), ("up", 50)]
    display.events.clear()
    hotkeys.send("ctrl+!", backend=backend)
    assert display.events == [("down", 37), ("down", 50), ("down", 10), ("up", 10), ("up", 50), ("up", 37)]
    display.events.clear()
    hotkeys.send("h", backend=backend)
    assert display.events == [("down", 43), ("up", 43)]


def test_xtest_right_alt_falls_back_to_the_altgr_key():
    """<summary>
    alt_r is sent as the physical right alt key when the keymap has no Alt_R.
    </summary>
    <remarks>
    On a UK or US X keymap the right alt key carries ISO_Level3_Shift and no
    key carries Alt_R, so a lookup by Alt_R alone finds nothing and the
    right alt bind in a game could never be sent. The fallback sends the key
    that is physically there, which is what the game sees from the keyboard.
    </remarks>
    """
    backend, display = xtest_backend()
    hotkeys.send("alt_r+h", backend=backend)
    assert display.events == [("down", 108), ("down", 43), ("up", 43), ("up", 108)]


def test_xtest_unknown_key_presses_nothing():
    """<summary>
    A key with no keycode on the layout raises before anything is pressed.
    </summary>
    <remarks>
    Keycodes are all resolved first, so ctrl+<missing> does not leave ctrl
    down in the X server with nothing to release it.
    </remarks>
    """
    backend, display = xtest_backend()
    with pytest.raises(hotkeys.HotkeyError):
        hotkeys.send("ctrl+media_volume_mute", backend=backend)
    assert display.events == []


def test_xtest_press_and_release_bracket_a_hold():
    """<summary>
    press leaves the keys down, release lets them go in reverse, shift
    included for a character that needed it.
    </summary>
    """
    backend, display = xtest_backend()
    hotkeys.press("alt+!", backend=backend)
    assert display.events == [("down", 64), ("down", 50), ("down", 10)]
    display.events.clear()
    hotkeys.release("alt+!", backend=backend)
    assert display.events == [("up", 10), ("up", 50), ("up", 64)]


def test_xtest_type_taps_each_character_as_its_key(monkeypatch):
    """<summary>
    Text is typed one real key tap per character, newline as enter, with a
    pause between characters that is never nothing.
    </summary>
    <remarks>
    Text into a game has the same problem hotkeys had: pynput's synthetic
    characters never arrive. Here each character is a tap of the key that
    makes it, which is what the chat box of a game sees from the keyboard.
    </remarks>
    """
    backend, display = xtest_backend()
    pauses = []
    monkeypatch.setattr(hotkeys.time, "sleep", pauses.append)
    hotkeys.type_text("hH\n", backend=backend)
    assert display.events == [("down", 43), ("up", 43),
                              ("down", 50), ("down", 43), ("up", 43), ("up", 50),
                              ("down", 36), ("up", 36)]
    assert pauses == [0.01, 0.01]


def test_default_backend_prefers_xtest_on_linux(monkeypatch):
    """<summary>
    On Linux the X test backend is tried first and pynput only when it cannot
    be built.
    </summary>
    <remarks>
    The order is the fix: a daemon that fell back to pynput on a machine with
    a working X server would quietly go back to letters that games never see.
    The constructors are replaced so the test touches no display.
    </remarks>
    """
    monkeypatch.setattr(hotkeys.sys, "platform", "linux")
    monkeypatch.setattr(hotkeys, "XTestBackend", lambda: "xtest")
    monkeypatch.setattr(hotkeys, "PynputBackend", lambda: "pynput")
    assert hotkeys.default_backend() == "xtest"

    def no_xtest():
        raise RuntimeError("no display")
    monkeypatch.setattr(hotkeys, "XTestBackend", no_xtest)
    assert hotkeys.default_backend() == "pynput"

    monkeypatch.setattr(hotkeys.sys, "platform", "win32")
    monkeypatch.setattr(hotkeys, "XTestBackend", lambda: pytest.fail("X test must not be tried on Windows"))
    assert hotkeys.default_backend() == "pynput"


def test_backend_choice_is_announced_once_through_the_given_log(monkeypatch):
    """<summary>
    The first shared backend built says which one it is through the callback
    registered with announce_to, and later calls say nothing more.
    </summary>
    <remarks>
    The daemon has no logging module behind it: its log is a callback that
    prints a timestamped line. A backend line written anywhere else never
    reaches run.log, which is how the first build of the X test backend went
    unannounced on 17/10/2026. The line names the backend in words, so the
    person reading the log does not need to know the class names.
    </remarks>
    """
    lines = []
    monkeypatch.setattr(hotkeys, "_backend", None)
    monkeypatch.setattr(hotkeys.sys, "platform", "linux")
    monkeypatch.setattr(hotkeys.XTestBackend, "__init__", lambda self: None)  # no display touched
    hotkeys.announce_to(lines.append)
    try:
        first = hotkeys._resolve_backend(None)
        second = hotkeys._resolve_backend(None)
    finally:
        hotkeys.announce_to(lambda text: None)
    assert first is second
    assert lines == ["hotkeys through the X test extension"]


def test_describe_names_each_backend_in_words():
    """<summary>Each real backend has a plain name and anything else falls back to its class.</summary>"""
    assert hotkeys.describe(hotkeys.XTestBackend.__new__(hotkeys.XTestBackend)) == "the X test extension"
    assert hotkeys.describe(hotkeys.XdotoolBackend()) == "xdotool"
    assert hotkeys.describe(object()) == "object"
