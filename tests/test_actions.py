"""<summary>
What a key press actually does: every action type, run through a runner
whose every outside effect is replaced by a recording fake.
</summary>
<remarks>
An action is the only part of Deckplate that reaches out of the process, so
the tests here are mostly about what got sent, in what order, and what
happened when it failed. No real key is typed, no process is launched and no
browser is opened. A regression that let one of these through would type
into whatever window has focus on the machine running the suite.

The runner takes its effects as constructor arguments, and several tests
also reassign them afterwards. That is deliberate rather than untidy: the
chord tests need to interleave presses, taps and waits in a single ordered
list, which cannot be done through separate recorders.
</remarks>
"""

import sys

from deckplate.actions import ActionRunner, platform_command
from deckplate.config import Action


class FakeContext:
    """<summary>
    Stands in for the controller, recording the things an action asks the
    deck itself to do.
    </summary>
    <remarks>
    These three are the actions that stay inside Deckplate rather than
    reaching the operating system, so they are recorded as calls on the
    context instead of through the effect callables. The brightness
    recording keeps both arguments, because an absolute value and a relative
    change are different intents and must not be flattened into one number.
    </remarks>
    """

    def __init__(self):
        """
        <summary>
        Start with an empty call log.
        </summary>
        """
        self.calls = []

    def switch_page(self, target):
        """<summary>Record a page change request.</summary>"""
        self.calls.append(("page", target))

    def set_brightness(self, value=None, delta=None):
        """<summary>Record a brightness change, absolute or relative.</summary>"""
        self.calls.append(("brightness", value, delta))

    def sleep_deck(self):
        """<summary>Record a request to blank the deck.</summary>"""
        self.calls.append(("sleep",))


def make_runner():
    """<summary>
    A runner with every outside effect replaced by a list that records it.
    </summary>
    <returns>The runner, its fake context, and the dictionary of recordings.</returns>
    <remarks>
    The ``sleep`` recorder is the subtle one: it captures the delay in
    seconds rather than waiting, which is both what keeps the suite fast and
    what makes the delays assertable. Timings are configured in
    milliseconds and recorded in seconds, so a conversion fault shows up as
    a wrong number here rather than as an action that feels sluggish months
    later.
    </remarks>
    """
    context = FakeContext()
    seen = {"hotkeys": [], "launched": [], "urls": [], "slept": [], "log": []}
    runner = ActionRunner(
        context,
        send_hotkey=seen["hotkeys"].append,
        launch=seen["launched"].append,
        open_url=seen["urls"].append,
        sleep=seen["slept"].append,
        log=seen["log"].append,
    )
    return runner, context, seen


def test_each_action_type():
    """<summary>
    Pins every simple action type to the one effect it is supposed to have,
    and pins the two brightness forms apart.
    </summary>
    <remarks>
    This is the breadth test that catches an action type quietly losing its
    dispatch entry. Each call returning True matters as much as the effect:
    the return says the action succeeded, and the controller uses it to
    decide whether to complain. An action that did nothing but reported
    success would be invisible to the user except as a dead key.

    The two brightness calls are checked as an ordered list rather than as
    membership, so an absolute set of 30 cannot be mistaken for a relative
    change of 30. Getting those the wrong way round would dim the deck by a
    step where the user asked for a level.
    </remarks>
    """
    runner, context, seen = make_runner()
    assert runner.run(Action("hotkey", {"keys": "ctrl+l"}))
    assert runner.run(Action("launch", {"command": "gedit"}))
    assert runner.run(Action("url", {"url": "https://example.org"}))
    assert runner.run(Action("page", {"page": "next"}))
    assert runner.run(Action("brightness", {"delta": -10}))
    assert runner.run(Action("brightness", {"value": 30}))
    assert runner.run(Action("sleep"))
    assert seen["hotkeys"] == ["ctrl+l"]
    assert seen["launched"] == ["gedit"]
    assert seen["urls"] == ["https://example.org"]
    assert context.calls == [("page", "next"), ("brightness", None, -10), ("brightness", 30, None), ("sleep",)]


def test_multi_runs_in_order_with_delays():
    """<summary>
    Pins a multi action as running its steps in order with a delay between
    them, and specifically not after the last one.
    </summary>
    <remarks>
    Three steps giving two delays is the assertion with teeth. A delay after
    the final step would add a quarter of a second of nothing to the end of
    every press, which compounds when a key is tapped repeatedly and feels
    like lag with no visible cause. The steps deliberately mix an operating
    system effect, a browser effect and a deck effect, proving a multi can
    drive all three paths rather than only the ones it shares a recorder
    with.
    </remarks>
    """
    runner, context, seen = make_runner()
    steps = (Action("hotkey", {"keys": "a"}), Action("url", {"url": "u"}), Action("sleep"))
    assert runner.run(Action("multi", {"steps": steps, "delay_ms": 250}))
    assert seen["hotkeys"] == ["a"] and seen["urls"] == ["u"] and context.calls == [("sleep",)]
    assert seen["slept"] == [0.25, 0.25]


def test_sequence_sends_keys_in_order_with_the_delay():
    """<summary>
    Pins a key sequence to its order and its gaps, including a zero delay
    meaning no wait at all.
    </summary>
    <remarks>
    A sequence exists for applications that drop keys sent back to back, so
    the gap is the feature rather than an implementation detail. Three keys
    give two gaps, for the same reason as a multi. The zero case is checked
    separately because sleeping for zero seconds still yields the thread to
    the scheduler, which on a busy machine is a real pause, so the call must
    be skipped outright rather than passed a zero.
    </remarks>
    """
    runner, _, seen = make_runner()
    assert runner.run(Action("sequence", {"keys": ("ctrl+l", "h", "enter"), "delay_ms": 80}))
    assert seen["hotkeys"] == ["ctrl+l", "h", "enter"]
    assert seen["slept"] == [0.08, 0.08]
    seen["slept"].clear()
    assert runner.run(Action("sequence", {"keys": ("a", "b"), "delay_ms": 0}))
    assert seen["slept"] == []


def test_chord_holds_the_first_key_for_the_whole_run():
    """<summary>
    Pins the full interleaving of a chord: the held key goes down first,
    every tap happens under it after a wait, and it is let go at the end.
    </summary>
    <remarks>
    A chord is for applications where a modifier must stay down across
    several taps, such as switching windows, so the whole behaviour is the
    ordering. Recording presses, taps and waits into one shared list is what
    makes that assertable; three separate recorders could not tell whether a
    tap landed inside or outside the hold.

    Note that a wait comes before each tap, including the first. That is not
    an accident: the application needs a moment after the modifier goes down
    before the first tap registers. The randomness is removed by an ``rng``
    that returns the midpoint, so the delay range of 100 to 300 always gives
    0.2 seconds. Red here with the right members but the wrong order means
    taps are escaping the hold, which in a real application does something
    entirely different from what the user bound.
    </remarks>
    """
    runner, _, seen = make_runner()
    order = []
    runner.press_keys = lambda c: order.append(("down", c))
    runner.release_keys = lambda c: order.append(("up", c))
    runner.send_hotkey = lambda c: order.append(("tap", c))
    runner.sleep = lambda s: order.append(("wait", round(s, 3)))
    runner.rng = lambda low, high: (low + high) / 2
    params = {"hold": "alt", "keys": ("1", "2", "ctrl+s"), "delay_min_ms": 100, "delay_max_ms": 300}
    assert runner.run(Action("chord", params))
    assert order == [
        ("down", "alt"),
        ("wait", 0.2), ("tap", "1"),
        ("wait", 0.2), ("tap", "2"),
        ("wait", 0.2), ("tap", "ctrl+s"),
        ("up", "alt"),
    ]


def test_chord_releases_the_held_key_even_if_a_tap_fails():
    """<summary>
    Pins the held key being let go when a tap in the middle of the chord
    throws.
    </summary>
    <remarks>
    This is the worst failure the action layer can produce. If the release
    were skipped, a modifier such as alt would be left down at the operating
    system level with nothing running that could lift it, so every
    subsequent keystroke the user typed anywhere would be modified. The
    release has to happen from a finally, not from the happy path.

    The action correctly reports failure while still having cleaned up, so
    both are asserted together: a version that swallowed the error and
    claimed success would be just as wrong.
    </remarks>
    """
    runner, _, seen = make_runner()
    order = []
    runner.press_keys = lambda c: order.append(("down", c))
    runner.release_keys = lambda c: order.append(("up", c))
    runner.send_hotkey = lambda c: (_ for _ in ()).throw(RuntimeError("no display"))
    runner.sleep = lambda s: None
    assert not runner.run(Action("chord", {"hold": "alt", "keys": ("1",), "delay_min_ms": 0, "delay_max_ms": 0}))
    assert order == [("down", "alt"), ("up", "alt")]


def test_chord_without_a_held_key_just_taps():
    """<summary>
    Pins an empty hold as meaning no key is held, rather than an empty
    combination being pressed.
    </summary>
    <remarks>
    The configuration page leaves the hold field blank when it is not
    wanted, so this is a shape that arrives from a normal saved layout, not
    an edge case. Pressing an empty combination is at best a no operation
    and at worst an exception from the backend, which would take the whole
    chord down with it.
    </remarks>
    """
    runner, _, seen = make_runner()
    runner.sleep = lambda s: None
    assert runner.run(Action("chord", {"hold": "", "keys": ("a", "b"), "delay_min_ms": 0, "delay_max_ms": 0}))
    assert seen["hotkeys"] == ["a", "b"]


def test_empty_key_actions_do_nothing():
    """<summary>
    Pins an action with nothing bound to it as a quiet success that sends
    no keys and starts no hold.
    </summary>
    <remarks>
    A key that has been added on the configuration page but not yet filled
    in produces exactly these shapes, and pressing it must be harmless. The
    two halves both matter: nothing may be sent, and each call must still
    report success, because reporting failure would log a complaint every
    time the user pressed an unfinished key.

    The hold case is the one worth guarding. An empty combination reaching
    the hold manager would create a thread keyed on the empty string that no
    later press could identify, so it could never be switched off.
    </remarks>
    """
    runner, _, seen = make_runner()
    toggles = []
    runner.holds = type("H", (), {"toggle": lambda self, *a: toggles.append(a), "active_combos": lambda self: set(),
                                  "stop_all": lambda self: None})()
    assert runner.run(Action("hotkey", {"keys": ""}))
    assert runner.run(Action("sequence", {"keys": (), "delay_ms": 100}))
    assert runner.run(Action("hold", {"keys": "", "hold_min_ms": 1, "hold_max_ms": 2, "release_min_ms": 0, "release_max_ms": 0}))
    assert seen["hotkeys"] == [] and toggles == []


def test_hold_action_toggles_through_the_manager():
    """<summary>
    Pins the hold action handing its four configured times to the manager as
    two ranges, in the right order.
    </summary>
    <remarks>
    The configuration stores four flat numbers and the manager wants a hold
    span and a release span. Pairing them wrongly does not raise anywhere:
    the hold simply behaves with the wrong rhythm, which is easy to miss and
    defeats the point of the feature. This test does not go near the
    threading, which belongs with the hold manager's own tests; it pins only
    the translation across the boundary.
    </remarks>
    """
    runner, _, seen = make_runner()
    toggles = []

    class FakeHolds:
        """
        <summary>
        Stand-in for the hold manager: records toggles and holds nothing.
        </summary>
        """
        def toggle(self, combo, hold_ms, release_ms):
            """
            <summary>
            Record the toggle and report it as started.
            </summary>
            <param name="combo">The combination.</param>
            <param name="hold_ms">Hold time.</param>
            <param name="release_ms">Release time.</param>
            <returns>True.</returns>
            """
            toggles.append((combo, hold_ms, release_ms))
            return True

        def active_combos(self):
            """
            <summary>
            Never any.
            </summary>
            <returns>An empty set.</returns>
            """
            return set()

        def stop_all(self):
            """
            <summary>
            Nothing to stop.
            </summary>
            """
            pass

    runner.holds = FakeHolds()
    params = {"keys": "w", "hold_min_ms": 3000, "hold_max_ms": 8000, "release_min_ms": 150, "release_max_ms": 600}
    assert runner.run(Action("hold", params))
    assert toggles == [("w", (3000, 8000), (150, 600))]


def test_failure_is_logged_not_raised():
    """<summary>
    Pins a failing effect as a False return and a log line carrying the
    cause, never an escaping exception.
    </summary>
    <remarks>
    Actions run on the deck's event loop, so an exception here would take
    the whole daemon down over one bad binding and every other key on the
    deck with it. The log line is checked for the original message rather
    than merely existing, because the user's only route to the cause is what
    the line says; a generic complaint that an action failed leaves them
    nothing to act on.
    </remarks>
    """
    runner, _, seen = make_runner()
    runner.send_hotkey = lambda keys: (_ for _ in ()).throw(RuntimeError("no display"))
    assert not runner.run(Action("hotkey", {"keys": "a"}))
    assert seen["log"] and "no display" in seen["log"][0]


def test_platform_command_override():
    """<summary>
    Pins the per platform command override, including the fall back when
    only the shared command is given.
    </summary>
    <remarks>
    One configuration file is meant to be carried between the Linux and
    Windows machines, so a launch action can name a different program on
    each. The expected value is computed from the running platform rather
    than hard coded, which is what lets this same assertion hold on both
    build hosts. Red here means a layout that works on one machine silently
    launches the wrong program, or nothing, on the other.
    </remarks>
    """
    params = {"command": "gedit", "command_windows": "notepad.exe", "command_linux": "xed"}
    expected = "notepad.exe" if sys.platform == "win32" else "xed"
    assert platform_command(params) == expected
    assert platform_command({"command": "shared"}) == "shared"
