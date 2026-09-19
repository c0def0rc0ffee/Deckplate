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
    </remarks>
    """
    assert hotkeys.parse_combo(text) == expected


@pytest.mark.parametrize("text", ["", "ctrl+", "ctrl+banana", "t+ctrl"])
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
