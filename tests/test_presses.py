"""<summary>
The long press and double press timing, driven by a fake clock.
</summary>
<remarks>
Time is passed in as a number on every call, so nothing here sleeps and the
suite stays fast and repeatable. The router is a state machine with one entry
per key in flight, and these tests are its specification.

What breaks in the real world when this goes red is subtle and maddening to
use: a key that fires twice, a long press that also fires the plain action, or
a tap that does nothing until the next tap. The timings in the assertions are
chosen to sit either side of a boundary on purpose, so a change from "at or
after" to "after" shows up here rather than in someone's hands.
</remarks>
"""

import pytest

from deckplate.config import Action
from deckplate.presses import Actions, PressRouter

PLAIN = Action("hotkey", {"keys": "a"})
LONG = Action("hotkey", {"keys": "b"})
DOUBLE = Action("hotkey", {"keys": "c"})


def router():
    """
    <summary>
    A PressRouter with a 400 ms long press and a 300 ms double press window.
    </summary>
    <returns>A PressRouter.</returns>
    """
    return PressRouter(long_ms=400, double_ms=300)


def test_a_plain_key_still_fires_on_the_press():
    """<summary>
    A key with only a plain action fires immediately and leaves nothing pending.
    </summary>
    <remarks>
    This is the common case and the one that has to stay instant. If configuring
    long or double presses anywhere caused every ordinary key to wait for a timer,
    the whole deck would feel laggy.
    </remarks>
    """
    r = router()
    assert r.press(0, 0, Actions(plain=PLAIN), 10.0) == [PLAIN]
    assert r.release(0, 0, 10.05) == []
    assert not r.pending
    assert r.due(10.05) is None


def test_a_key_with_nothing_on_it_does_nothing():
    """<summary>
    A key with no actions configured produces nothing on press or release.
    </summary>
    <remarks>
    Empty positions are normal, not an error, and they must not leave state behind
    that a later press on another key could pick up.
    </remarks>
    """
    r = router()
    assert r.press(0, 0, Actions(), 10.0) == []
    assert r.release(0, 0, 10.1) == []


def test_a_short_press_on_a_long_key_fires_on_the_release():
    """<summary>
    When a key also has a long action, the plain action waits for the release.
    </summary>
    <remarks>
    It has to: firing on the press would mean every long press ran the plain action
    first. The cost is that these keys feel slightly later than plain ones, which
    is the deliberate trade and the reason not every key is treated this way.
    </remarks>
    """
    r = router()
    assert r.press(0, 0, Actions(plain=PLAIN, long=LONG), 10.0) == []
    assert r.tick(10.2) == []
    assert r.release(0, 0, 10.2) == [PLAIN]
    assert not r.pending


def test_the_long_action_fires_while_the_key_is_still_down():
    """<summary>
    The long action fires as the threshold passes, exactly once, and the release
    afterwards adds nothing.
    </summary>
    <remarks>
    Firing while the key is still down is what makes a long press feel right, and
    it is why the run loop has to tick the router rather than only reacting to
    hardware events. The two assertions after the threshold are the real content:
    repeated ticks must not fire it again, and the eventual release must not add
    the plain action on top.
    </remarks>
    """
    r = router()
    r.press(0, 0, Actions(plain=PLAIN, long=LONG), 10.0)
    assert r.tick(10.39) == []
    assert r.tick(10.4) == [LONG]
    assert r.tick(10.9) == []          # only once
    assert r.release(0, 0, 11.0) == []  # and the release adds nothing


def test_the_loop_is_told_when_the_long_press_is_due():
    """<summary>
    The router reports how long until its next deadline, and None when there is
    none.
    </summary>
    <remarks>
    This is what lets the run loop block on the hardware read instead of spinning:
    it waits for the shorter of the deck's own timeout and this deadline. If None
    were returned while a long press were still pending, the action would not fire
    until the next unrelated event woke the loop.
    </remarks>
    """
    r = router()
    r.press(0, 0, Actions(plain=PLAIN, long=LONG), 10.0)
    assert r.due(10.0) == pytest.approx(0.4)
    assert r.due(10.3) == pytest.approx(0.1)
    r.tick(10.5)
    assert r.due(10.5) is None


def test_two_presses_inside_the_window_are_a_double():
    """<summary>
    A second press inside the window fires the double action, and the plain action
    is dropped rather than delayed.
    </summary>
    <remarks>
    The final tick is the point of the test. The plain action from the first press
    must be cancelled outright, because a plain action arriving after the double
    one would mean every double press did both things in an order the user never
    asked for.
    </remarks>
    """
    r = router()
    actions = Actions(plain=PLAIN, double=DOUBLE)
    assert r.press(0, 0, actions, 10.0) == []
    assert r.release(0, 0, 10.05) == []   # waiting to see if a second comes
    assert r.press(0, 0, actions, 10.2) == [DOUBLE]
    assert r.release(0, 0, 10.25) == []
    assert not r.pending
    assert r.tick(11.0) == []             # the plain action was dropped, not delayed


def test_one_press_on_a_double_key_fires_the_plain_action_when_the_window_closes():
    """<summary>
    A single press on a key that also has a double action fires the plain action
    once the window has closed.
    </summary>
    <remarks>
    This is the cost of configuring a double press: the plain action on that key is
    late by the width of the window. It has to happen, since there is no way to
    know a second press is not coming, but it is why the window is kept short.
    </remarks>
    """
    r = router()
    actions = Actions(plain=PLAIN, double=DOUBLE)
    r.press(0, 0, actions, 10.0)
    r.release(0, 0, 10.05)
    assert r.tick(10.3) == []
    assert r.tick(10.4) == [PLAIN]
    assert not r.pending


def test_a_second_press_is_not_also_a_long_press():
    """<summary>
    The press that completes a double gesture does not start a long press timer of
    its own.
    </summary>
    <remarks>
    Without this, holding the second press of a double would fire the double action
    and then the long one. The trailing tick past the long threshold is what proves
    the timer was never armed.
    </remarks>
    """
    r = router()
    actions = Actions(plain=PLAIN, long=LONG, double=DOUBLE)
    r.press(0, 0, actions, 10.0)
    r.release(0, 0, 10.05)
    assert r.press(0, 0, actions, 10.2) == [DOUBLE]
    assert r.tick(11.0) == []


def test_a_long_press_beats_the_double_window():
    """<summary>
    On a key configured with both, holding past the long threshold fires the long
    action and the release does not then open a double window.
    </summary>
    <remarks>
    The gesture is settled the moment the long action fires. Leaving the double
    window open afterwards would make a long press followed by a normal tap read as
    a double press, which is easy to do by accident and hard to explain.
    </remarks>
    """
    r = router()
    actions = Actions(plain=PLAIN, long=LONG, double=DOUBLE)
    r.press(0, 0, actions, 10.0)
    assert r.tick(10.4) == [LONG]
    assert r.release(0, 0, 10.6) == []
    assert not r.pending


def test_two_keys_are_timed_apart():
    """<summary>
    Each key in flight keeps its own timing state.
    </summary>
    <remarks>
    State is kept per position, not globally, so pressing a second key does not
    cancel or reset the gesture on the first. With one shared timer, holding one
    key and tapping another would lose the long press entirely.
    </remarks>
    """
    r = router()
    r.press(0, 0, Actions(plain=PLAIN, long=LONG), 10.0)
    r.press(1, 2, Actions(plain=DOUBLE, long=LONG), 10.3)
    assert r.tick(10.4) == [LONG]
    assert r.release(0, 0, 10.5) == []
    assert r.release(1, 2, 10.5) == [DOUBLE]


def test_clearing_forgets_a_gesture_in_flight():
    """<summary>
    Clearing drops every gesture in flight, including the pending deadline.
    </summary>
    <remarks>
    This is what a page switch and a sleep call, and it must be complete. A gesture
    surviving a page switch would fire an action belonging to the old page onto the
    new one, which is the wrong action entirely rather than a late one.
    </remarks>
    """
    r = router()
    r.press(0, 0, Actions(plain=PLAIN, long=LONG), 10.0)
    r.clear()
    assert r.tick(11.0) == []
    assert r.release(0, 0, 11.0) == []
    assert r.due(11.0) is None


def test_the_window_is_told_how_long_it_has_left():
    """<summary>
    The double press window only becomes a deadline once the key has come back up.
    </summary>
    <remarks>
    Nothing is due while the key is still held, because the gesture could still turn
    into a long press. Reporting a deadline too early would have the run loop wake
    and settle the gesture as a plain press while the finger was still down.
    </remarks>
    """
    r = router()
    actions = Actions(plain=PLAIN, double=DOUBLE)
    r.press(0, 0, actions, 10.0)
    assert r.due(10.0) is None       # nothing is due until the key comes up
    r.release(0, 0, 10.0)
    assert r.due(10.1) == pytest.approx(0.2)
