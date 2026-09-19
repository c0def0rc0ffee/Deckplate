"""<summary>
Hold toggles, with a fake keyboard and short times so the tests run fast.
</summary>
<remarks>
Everything under test here runs on its own thread, so the assertions are
written as waits rather than as sleeps followed by a check: the timings are
milliseconds and a fixed sleep would be flaky on a loaded machine. The
generous three second ceiling in <see cref="wait_for"/> is the failure
budget, not the expected wait.

The random timing is made deterministic by handing the manager an ``rng``
that always returns the midpoint of its span, so a test that asks for 20 to
40 milliseconds always gets 30. That is what makes cycle counts assertable.

No real keyboard is ever driven. A regression that let one of these threads
reach the real backend would start typing into whatever window has focus on
the machine running the suite.
</remarks>
"""

import threading
import time

from deckplate.holds import HoldManager


class FakeKeyboard:
    """<summary>
    Stands in for the keyboard backend and records every press and release
    in order.
    </summary>
    <remarks>
    The lock is not decoration. Hold, boost and repeat each run on their own
    thread and the test body reads the log from the main thread, so the list
    genuinely is touched from several threads at once. Always read through
    <see cref="FakeKeyboard.snapshot"/>, which copies under the lock: a test
    that iterates ``self.log`` directly can see it mutate mid pass and fail
    for a reason that has nothing to do with the code under test.
    </remarks>
    """

    def __init__(self):
        """
        <summary>
        Start with an empty log and its lock.
        </summary>
        """
        self.log = []
        self.lock = threading.Lock()

    def press(self, combo):
        """<summary>Record a key going down.</summary>"""
        with self.lock:
            self.log.append(("press", combo))

    def release(self, combo):
        """<summary>Record a key being let go.</summary>"""
        with self.lock:
            self.log.append(("release", combo))

    def snapshot(self):
        """<summary>
        A copy of the log so far, safe to read while the hold threads run.
        </summary>
        <returns>A new list of (kind, combo) pairs in the order they happened.</returns>
        """
        with self.lock:
            return list(self.log)


def wait_for(predicate, timeout=3):
    """
    <summary>
    Poll a predicate until it is true or the timeout passes.
    </summary>
    <param name="predicate">Called until it returns true.</param>
    <param name="timeout">Seconds.</param>
    <returns>True when it became true in time.</returns>
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make():
    """<summary>
    A fake keyboard and a manager wired to it, with the randomness removed.
    </summary>
    <returns>The keyboard and the manager, in that order.</returns>
    <remarks>
    The ``rng`` always returns the midpoint of the span it is given, which
    is what turns a deliberately random cycle into something a test can
    count. Logging is swallowed, so a test that cares about the log builds
    its own manager instead of calling this.
    </remarks>
    """
    keyboard = FakeKeyboard()
    manager = HoldManager(press=keyboard.press, release=keyboard.release,
                          rng=lambda low, high: (low + high) / 2, log=lambda t: None)
    return keyboard, manager


def test_toggle_holds_then_releases():
    """<summary>
    Pins the basic contract of a hold: one press starts it, the next stops
    it, and the key is let go exactly once.
    </summary>
    <remarks>
    The hold span is a minute so nothing can cycle during the test, which
    makes the expected log exactly one press and one release. That equality
    is the real assertion: a stray extra release would mean the key is let
    go twice, and a missing one would mean the key stays down in the game
    forever with no deck key left to clear it. The returned booleans matter
    too, because the controller uses them to decide whether to light the key
    as running.
    </remarks>
    """
    keyboard, manager = make()
    assert manager.toggle("w", (60_000, 60_000), (0, 0)) is True
    assert wait_for(lambda: ("press", "w") in keyboard.snapshot())
    assert manager.active("w") and manager.active_combos() == {"w"}
    assert manager.toggle("w", (60_000, 60_000), (0, 0)) is False
    assert keyboard.snapshot() == [("press", "w"), ("release", "w")]
    assert not manager.active("w")


def test_random_release_cycle_lets_go_and_presses_again():
    """<summary>
    Pins the whole point of a hold with a release range: the key is let go
    and pressed again over and over rather than being held flat.
    </summary>
    <remarks>
    A key held down without a break reads as a taped key, which is exactly
    what this feature exists to avoid, so if this went red the hold would
    still work and would still look wrong to anything watching. The order of
    the first three entries is checked rather than only the count, because a
    cycle that released before it ever pressed would give the same tally.
    The final assertion covers the stop landing mid cycle: whatever phase it
    was in, the key must end up released.
    </remarks>
    """
    keyboard, manager = make()
    manager.toggle("w", (20, 40), (10, 20))
    assert wait_for(lambda: keyboard.snapshot().count(("press", "w")) >= 3)
    log = keyboard.snapshot()
    assert log[0] == ("press", "w") and log[1] == ("release", "w") and log[2] == ("press", "w")
    manager.toggle("w", (20, 40), (10, 20))
    assert keyboard.snapshot()[-1] == ("release", "w")


def test_plain_hold_never_releases_until_stopped():
    """<summary>
    Pins a zero release range as meaning a genuinely unbroken hold.
    </summary>
    <remarks>
    This is the documented way to ask for a plain hold, and it is the mode
    that matters most in a game: a single dropped frame of the key would
    read as letting go of the throttle. The hold span is short on purpose,
    so several cycles would have elapsed by the time of the check if the
    zero range were being ignored. It also proves a multi key combination
    survives as one unit through the manager rather than being split.
    </remarks>
    """
    keyboard, manager = make()
    manager.toggle("shift+w", (20, 30), (0, 0))
    time.sleep(0.15)
    assert ("release", "shift+w") not in keyboard.snapshot()
    manager.stop_all()
    assert keyboard.snapshot()[-1] == ("release", "shift+w")
    assert manager.active_combos() == set()


def test_stop_all_releases_every_hold():
    """<summary>
    Pins the panic path: stopping everything lets go of every key that is
    down, not just the most recent one.
    </summary>
    <remarks>
    This runs on shutdown and on a configuration reload, and it is the last
    chance to undo anything the daemon did to the user's keyboard. A hold
    left behind here means a key stuck down after Deckplate has exited, with
    nothing left running that could release it, which is the worst failure
    this module can produce. Two independent holds are used because the
    plausible bug is a loop that mutates the manager's own collection while
    iterating it and quietly stops only the first.
    </remarks>
    """
    keyboard, manager = make()
    manager.toggle("w", (60_000, 60_000), (0, 0))
    manager.toggle("a", (60_000, 60_000), (0, 0))
    assert wait_for(lambda: len(keyboard.snapshot()) == 2)
    manager.stop_all()
    released = {combo for kind, combo in keyboard.snapshot() if kind == "release"}
    assert released == {"w", "a"}


def test_backend_failure_is_logged_not_raised():
    """<summary>
    Pins a dead keyboard backend as a logged complaint rather than an
    exception nobody catches.
    </summary>
    <remarks>
    The failure is raised inside a worker thread, where an escape would be
    printed by the interpreter and lost, and the daemon would carry on
    believing the hold is running. Losing the display or the input backend
    mid session is a real occurrence, not a hypothetical. The message itself
    is checked for the cause, because a log line that only says a hold
    stopped tells the user nothing they can act on.
    </remarks>
    """
    log = []

    def bad_press(combo):
        """
        <summary>
        A press that raises as if there were no display.
        </summary>
        <param name="combo">Ignored.</param>
        <exception cref="RuntimeError">Always.</exception>
        """
        raise RuntimeError("no display")

    manager = HoldManager(press=bad_press, release=lambda c: None, log=log.append)
    manager.toggle("w", (10, 10), (0, 0))
    assert wait_for(lambda: any("no display" in line for line in log))


def test_boost_holds_one_key_and_pulses_another_until_stopped():
    """<summary>
    Forward stays down while the boost key goes on and off, and both are
    let go.
    </summary>
    <remarks>
    Boost is two keys with different jobs on one thread, so the ordering is
    the whole behaviour. The held key must go down first, before any boost
    pulse, or the first pulse happens with no movement under it. It must
    then stay down across at least two complete boost cycles, which is what
    proves the pulse loop is not dragging the hold with it.

    On stop the boost is released before the held key, so the order of the
    tail is asserted rather than just its membership: releasing forward
    first would leave a sprint modifier down for a moment with nothing being
    sprinted. This test builds its own manager because boost also needs a
    ``send`` callable, which <see cref="make"/> does not supply.
    </remarks>
    """
    keyboard = FakeKeyboard()
    manager = HoldManager(press=keyboard.press, release=keyboard.release,
                          send=lambda combo: None, rng=lambda low, high: (low + high) / 2,
                          log=lambda t: None)
    assert manager.toggle_boost("w", "shift", 40, 30) is True
    # forward goes down first and stays down while the boost cycles
    assert wait_for(lambda: keyboard.snapshot()[:1] == [("press", "w")])
    assert wait_for(lambda: sum(1 for e in keyboard.snapshot() if e == ("release", "shift")) >= 2)
    assert ("release", "w") not in keyboard.snapshot()
    assert manager.toggle_boost("w", "shift", 40, 30) is False
    # both keys are let go when it stops, boost first then the held key
    assert wait_for(lambda: ("release", "w") in keyboard.snapshot())
    tail = [e for e in keyboard.snapshot() if e[1] in ("w", "shift")]
    assert tail[-1] == ("release", "w")


def test_repeat_taps_on_a_timer_until_stopped():
    """<summary>
    Pins repeat as taps sent through the send path, and pins that switching
    it off really does end the thread.
    </summary>
    <remarks>
    Repeat is a tap, not a hold, so it goes through ``send`` rather than
    press and release: a repeat that pressed without releasing would look
    identical for one tap and then stick. The interesting half is after the
    toggle off, where the count is read, a further period is allowed to pass
    and the count must not have moved. Without that wait a thread that
    ignored its stop event would still pass, and in real use it would tap
    the key forever with no deck key bound to it any more.
    </remarks>
    """
    taps = []
    manager = HoldManager(press=lambda c: None, release=lambda c: None,
                          send=taps.append, rng=lambda low, high: (low + high) / 2,
                          log=lambda t: None)
    assert manager.toggle_repeat("f", 30) is True
    assert wait_for(lambda: len(taps) >= 3)
    assert manager.toggle_repeat("f", 30) is False
    settled = len(taps)
    time.sleep(0.15)
    assert len(taps) == settled  # nothing more once it is switched off
    assert set(taps) == {"f"}


def test_toggling_actions_have_their_own_keys():
    """<summary>
    Pins the identity used to track a running toggle, including the empty
    string that means an action does not toggle at all.
    </summary>
    <remarks>
    Two things hang off this key. Any deck key bound to the same thing must
    switch off the same task, so the key is derived from the parameters and
    never from the deck position. And the configuration page asks the same
    question to decide which keys to draw with a running border.

    The prefixes keep the namespaces apart: a plain hold of "w" and a repeat
    of "w" are different tasks and must not cancel each other, which is why
    a hold is the bare combination while the others are prefixed. A hotkey
    returns empty because it fires once and is never running, and code that
    treats empty as a valid key would make every hotkey on the deck share
    one slot.
    </remarks>
    """
    from deckplate.holds import task_key
    assert task_key("hold", {"keys": "w"}) == "w"
    assert task_key("boost", {"hold": "w", "keys": "shift"}) == "boost:w+shift"
    assert task_key("repeat", {"keys": "f"}) == "repeat:f"
    assert task_key("hotkey", {"keys": "f"}) == ""
