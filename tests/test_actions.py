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

import pytest

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


def test_launch_with_no_command_does_nothing():
    """<summary>
    Pins an empty launch command, the half filled in key from the page, as a
    success that launches nothing.
    </summary>
    <remarks>
    Launching an empty string would raise on Linux and open nothing useful on
    Windows, and either way the key would look broken rather than unfinished.
    True is the right return: nothing failed, there was simply nothing to do.
    </remarks>
    """
    runner, _, seen = make_runner()
    assert runner.run(Action("launch", {"command": ""}))
    assert runner.run(Action("launch", {"command": "", "command_linux": "", "command_windows": ""}))
    assert seen["launched"] == [] and seen["log"] == []


def test_text_action_types_then_optionally_presses_enter():
    """<summary>
    Pins a text action typing its text with its delay, pressing enter only
    when asked, and pressing enter alone when the text is empty.
    </summary>
    <remarks>
    Enter is sent as a hotkey and not as a newline in the text, so it goes
    through the same path every other keystroke does and a backend that
    treats a newline oddly cannot swallow it. Empty text with enter must
    still press enter: a key whose whole job is to send a return is a real
    use, and the empty text check exists only so the half filled in key from
    the page types nothing.
    </remarks>
    """
    runner, _, seen = make_runner()
    typed = []
    runner.type_text = lambda text, delay_ms: typed.append((text, delay_ms))
    assert runner.run(Action("text", {"text": "hello", "enter": False, "delay_ms": 0}))
    assert runner.run(Action("text", {"text": "gg", "enter": True, "delay_ms": 30}))
    assert runner.run(Action("text", {"text": "", "enter": True, "delay_ms": 0}))
    assert typed == [("hello", 0), ("gg", 30)]
    assert seen["hotkeys"] == ["enter", "enter"]


def test_request_action_sends_what_was_configured_with_the_token():
    """<summary>
    Pins a request action passing its method, address, headers, body and
    timeout through, and adding the token file's contents as Authorization.
    </summary>
    <remarks>
    The token is read through a seam and never appears in the action's
    parameters, which is the property that keeps a secret out of the config
    file, the page and the log. The headers the user wrote are kept and the
    Authorization header is added beside them rather than replacing them, so
    a custom header and a token can be used together. A request with no
    token file must not call the reader at all, since there is no file to
    read and a failure there would fail a request that needed no token.
    </remarks>
    """
    runner, _, seen = make_runner()
    sent, read = [], []
    runner.send_request = lambda method, url, headers, body, timeout: sent.append((method, url, headers, body, timeout))
    runner.read_token = lambda path: read.append(path) or "Bearer abc"
    assert runner.run(Action("request", {"url": "https://example.org/api", "method": "POST",
                                         "headers": {"X-Test": "1"}, "body": '{"a": 1}',
                                         "token_file": "/somewhere/token", "timeout_s": 5}))
    assert runner.run(Action("request", {"url": "https://example.org/ping", "method": "GET", "timeout_s": 10}))
    assert sent == [
        ("POST", "https://example.org/api", {"X-Test": "1", "Authorization": "Bearer abc"}, '{"a": 1}', 5),
        ("GET", "https://example.org/ping", {}, None, 10),
    ]
    assert read == ["/somewhere/token"]


def test_request_failure_is_logged_not_raised():
    """<summary>
    Pins a refused request as a False return with the cause in the log.
    </summary>
    <remarks>
    A request reaches the network, which is the least reliable thing an
    action does, so this is the type most likely to fail on an ordinary day.
    It has to fail the same way every other action does: quietly, logged,
    with the deck still working.
    </remarks>
    """
    runner, _, seen = make_runner()
    runner.send_request = lambda *args: (_ for _ in ()).throw(RuntimeError("503 Service Unavailable"))
    assert not runner.run(Action("request", {"url": "https://example.org", "method": "GET", "timeout_s": 10}))
    assert seen["log"] and "503" in seen["log"][0]


def test_read_token_adds_bearer_unless_a_scheme_is_written(tmp_path):
    """<summary>
    Pins the token file rule: a bare token gets Bearer in front, text with a
    space is used as it stands, whitespace is stripped, and an empty file is
    refused.
    </summary>
    <remarks>
    The trailing newline in the first file is deliberate, because every
    editor leaves one and a token sent with a newline on the end is refused
    by the server with a message that does not say why. The empty file case
    must raise rather than send "Bearer " with nothing after it, which would
    be a request that fails at the far end for a reason invisible here.
    </remarks>
    """
    from deckplate.actions import read_token
    bare = tmp_path / "bare"
    bare.write_text("abc123\n")
    basic = tmp_path / "basic"
    basic.write_text("  Basic dXNlcjpwYXNz  ")
    empty = tmp_path / "empty"
    empty.write_text("\n")
    assert read_token(str(bare)) == "Bearer abc123"
    assert read_token(str(basic)) == "Basic dXNlcjpwYXNz"
    with pytest.raises(ValueError):
        read_token(str(empty))


def test_default_request_sends_json_body_and_raises_on_error_status(monkeypatch):
    """<summary>
    Pins the real sender: the body goes as UTF-8 bytes with a JSON content
    type unless one was given, no body means no content type, and an error
    status is raised rather than returned.
    </summary>
    <remarks>
    The requests library is replaced at its one entry point so nothing
    leaves the machine. The content type rule is checked both ways because
    the wrong default is silent: a server that wanted a form and got JSON
    answers 400 with the body ignored, and the key looks dead. The error
    status is the reason the response is looked at at all, since the action
    has no other way to learn the server said no.
    </remarks>
    """
    from deckplate import actions

    calls = []

    class Response:
        """<summary>The two things the sender touches on a response.</summary>"""

        def __init__(self, status):
            """<summary>Remember the status.</summary>"""
            self.status = status

        def raise_for_status(self):
            """<summary>Raise the way requests does for 400 and above.</summary>"""
            if self.status >= 400:
                raise actions.requests.HTTPError(f"{self.status} error")

    def fake_request(method, url, headers=None, data=None, timeout=None):
        """<summary>Record the call and answer with the queued status.</summary>"""
        calls.append((method, url, headers, data, timeout))
        return Response(statuses.pop(0))

    statuses = [200, 200, 200, 404]
    monkeypatch.setattr(actions.requests, "request", fake_request)
    actions.default_request("POST", "https://example.org/a", {}, '{"x": 1}', 5)
    actions.default_request("POST", "https://example.org/b", {"content-type": "text/plain"}, "hi", 5)
    actions.default_request("GET", "https://example.org/c", {"X-A": "1"}, None, 7)
    with pytest.raises(actions.requests.HTTPError):
        actions.default_request("GET", "https://example.org/d", {}, None, 5)
    assert calls[0] == ("POST", "https://example.org/a", {"Content-Type": "application/json"}, b'{"x": 1}', 5)
    assert calls[1] == ("POST", "https://example.org/b", {"content-type": "text/plain"}, b"hi", 5)
    assert calls[2] == ("GET", "https://example.org/c", {"X-A": "1"}, None, 7)


class FakeAudio:
    """<summary>A sound backend that records what it was asked and can be told to fail.</summary>"""

    def __init__(self, fail=False, outputs=(), default=None):
        """<summary>Start with a call log, and optionally refuse everything.</summary>"""
        from deckplate.audio import AudioError, Output
        self.calls = []
        self.fail = fail
        self.error = AudioError("no pycaw")
        self._outputs = [Output(*o) for o in outputs]
        self._default = default

    def _do(self, *call):
        """<summary>Record, or raise when told to fail.</summary>"""
        if self.fail:
            raise self.error
        self.calls.append(call)

    def set_volume(self, percent): self._do("set", percent)
    def change_volume(self, delta): self._do("change", delta)
    def set_mute(self, mode): self._do("mute", mode)
    def outputs(self): return list(self._outputs)
    def default_output(self): return self._default
    def set_output(self, output_id): self.calls.append(("output", output_id))


def test_volume_action_uses_the_backend_and_falls_back_to_media_keys():
    """<summary>
    Pins each volume form reaching the backend, and pins the fallback when
    the backend refuses: a step becomes media key presses, a mute the media
    mute key, and an absolute level fails outright.
    </summary>
    <remarks>
    The fallback exists for Windows without pycaw, where the media keys
    still work. Two percent a press is how Windows counts a media key, so a
    change of ten is five presses. The absolute level has no fallback and
    must report failure rather than pretend, since nothing was set.
    </remarks>
    """
    runner, _, seen = make_runner()
    good = FakeAudio()
    runner._audio = good
    assert runner.run(Action("volume", {"value": 40}))
    assert runner.run(Action("volume", {"delta": -5}))
    assert runner.run(Action("volume", {"mute": "toggle"}))
    assert good.calls == [("set", 40), ("change", -5), ("mute", "toggle")]
    runner._audio = FakeAudio(fail=True)
    assert runner.run(Action("volume", {"delta": 10}))
    assert runner.run(Action("volume", {"delta": -3}))
    assert runner.run(Action("volume", {"mute": "on"}))
    assert seen["hotkeys"] == ["media_volume_up"] * 5 + ["media_volume_down"] * 2 + ["media_volume_mute"]
    assert not runner.run(Action("volume", {"value": 40}))
    assert any("no pycaw" in line for line in seen["log"])


def test_audio_output_action_picks_by_pattern_or_cycles():
    """<summary>
    Pins an output key switching to the first output matching its pattern,
    cycling to the one after the current, and logging rather than failing
    when nothing matches.
    </summary>
    """
    runner, _, seen = make_runner()
    audio = FakeAudio(outputs=[("sink_a", "Built-in Audio"), ("sink_b", "Headphones")], default="sink_a")
    runner._audio = audio
    assert runner.run(Action("audio_output", {"device": "head"}))
    assert runner.run(Action("audio_output", {"cycle": True}))
    assert runner.run(Action("audio_output", {"device": "nothing"}))
    assert audio.calls == [("output", "sink_b"), ("output", "sink_b")]
    assert any("nothing matches 'nothing'" in line and "Headphones" in line for line in seen["log"])


def test_window_action_acts_on_a_window_or_launches_the_program():
    """<summary>
    Pins a window key acting on the window found, launching its command only
    when nothing matched and the operation is focus, and logging when there
    is nothing to launch.
    </summary>
    <remarks>
    Launching on a failed minimise would start a program the user was
    trying to put away, so the launch is tied to focus alone.
    </remarks>
    """
    class FakeDesktop:
        """<summary>A desktop with one window, recording what is done to it.</summary>"""

        def __init__(self):
            """<summary>Start with an empty log.</summary>"""
            self.done = []

        def find(self, pattern):
            """<summary>Only "code" exists.</summary>"""
            return "77" if pattern == "code" else None

        def focus(self, w): self.done.append(("focus", w))
        def minimise(self, w): self.done.append(("minimise", w))
        def maximise(self, w): self.done.append(("maximise", w))
        def close(self, w): self.done.append(("close", w))

    runner, _, seen = make_runner()
    desktop = FakeDesktop()
    runner._desktop = desktop
    assert runner.run(Action("window", {"match": "code", "operation": "focus", "command": "code"}))
    assert runner.run(Action("window", {"match": "code", "operation": "close"}))
    assert runner.run(Action("window", {"match": "gimp", "operation": "focus", "command": "gimp"}))
    assert runner.run(Action("window", {"match": "gimp", "operation": "minimise", "command": "gimp"}))
    assert runner.run(Action("window", {"match": "gimp", "operation": "focus"}))
    assert desktop.done == [("focus", "77"), ("close", "77")]
    assert seen["launched"] == ["gimp"]
    assert any("nothing matches 'gimp'" in line for line in seen["log"])


def test_positional_actions_are_refused_by_the_runner():
    """<summary>
    Pins a toggle, timer, stopwatch or counter reaching the runner as a
    logged failure, since only the controller knows which key they belong to.
    </summary>
    """
    runner, _, seen = make_runner()
    for kind in ("toggle", "timer", "stopwatch", "counter"):
        assert not runner.run(Action(kind, {}))
    assert len(seen["log"]) == 4 and "controller" in seen["log"][0]
