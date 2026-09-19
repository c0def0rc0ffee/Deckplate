"""<summary>
The live key states: toggles, counters, timers and stopwatches, driven by a
clock the test moves by hand.
</summary>
<remarks>
Every assertion here is about what a key would show and when. The clock is a
plain number passed in, so a five minute timer is tested in the time it
takes to add three hundred to a float, and the wall clock never enters into
it.
</remarks>
"""

from deckplate import live


def test_format_seconds_cuts_rather_than_rounds():
    """<summary>
    Times read as m:ss below an hour and h:mm:ss above, with fractions cut.
    </summary>
    <remarks>
    Cutting is what makes a countdown honest: 0:00 must mean the time is up,
    so 0.9 seconds left reads 0:00 only if it rounds, which is wrong, and
    reads 0:00 on cutting only when it really is below one second.
    </remarks>
    """
    assert live.format_seconds(0) == "0:00"
    assert live.format_seconds(4.9) == "0:04"
    assert live.format_seconds(65) == "1:05"
    assert live.format_seconds(3600) == "1:00:00"
    assert live.format_seconds(-3) == "0:00"


def test_toggle_and_counter():
    """<summary>
    A toggle flips on each press and reads without changing; a counter moves
    by its step, goes negative, and resets to zero.
    </summary>
    """
    states = live.KeyStates()
    key = ("Main", 0, 0)
    assert not states.is_on(key)
    assert states.toggle(key) is True
    assert states.is_on(key)
    assert states.toggle(key) is False
    other = ("Main", 0, 1)
    assert states.count(other, 1) == 1
    assert states.count(other, 5) == 6
    assert states.count(other, -10) == -4
    assert states.count(other, reset=True) == 0
    # the toggle and the counter did not share a state
    assert not states.is_on(other)


def test_timer_counts_down_pauses_finishes_once_and_returns_to_idle():
    """<summary>
    A timer shows its length while idle, counts down while running, pauses
    and resumes on presses, finishes once, shows 0:00 in alert for a while,
    and then goes back to idle.
    </summary>
    <remarks>
    Finishing once is the property that matters most: the done action hangs
    off it, and a settle that reported the finish on every tick would fire
    that action ten times a second. The alert face and the return to idle
    are what the user sees, and both are pinned by the clock.
    </remarks>
    """
    states = live.KeyStates()
    key = ("Main", 1, 1)
    clock = states.clock(key, 300)
    assert clock.face(0) == live.Face(text="5:00", ring=1.0, active=False)
    clock.press(10)
    assert clock.face(70).text == "4:00" and clock.face(70).active
    assert abs(clock.face(70).ring - 240 / 300) < 1e-9
    clock.press(70)  # pause
    assert clock.face(100).text == "4:00" and not clock.face(100).active
    clock.press(100)  # resume
    assert clock.face(160).text == "3:00"
    assert states.settle(200) == []
    finished = states.settle(400)
    assert finished == [key]
    assert states.settle(401) == []  # reported once
    assert clock.face(401) == live.Face(text="0:00", ring=1.0, alert=True, active=False)
    assert states.any_running("Main")
    states.settle(401 + live.FINISHED_SECONDS)
    assert clock.face(420) == live.Face(text="5:00", ring=1.0, active=False)
    assert not states.any_running("Main")
    # a changed length resets the timer to the new length
    assert states.clock(key, 60).face(500).text == "1:00"


def test_stopwatch_runs_and_its_ring_sweeps_once_a_minute():
    """<summary>
    A stopwatch shows 0:00 with no ring while idle, counts up while running
    with a ring that completes every minute, and a press pauses it.
    </summary>
    """
    states = live.KeyStates()
    key = ("Main", 2, 2)
    clock = states.clock(key, None)
    assert clock.face(0) == live.Face(text="0:00", ring=None, active=False)
    clock.press(5)
    face = clock.face(35)
    assert face.text == "0:30" and face.active and abs(face.ring - 0.5) < 1e-9
    assert clock.face(65).text == "1:00" and abs(clock.face(65).ring) < 1e-9
    clock.press(65)
    assert clock.face(200).text == "1:00" and not clock.face(200).active
    assert states.settle(200) == []
    clock.reset()
    assert clock.face(300).text == "0:00"
