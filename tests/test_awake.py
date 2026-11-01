"""<summary>
The Stay Awake and Do Not Disturb threads, run against recorders so no
inhibit is taken on the real desktop and no pointer moves.
</summary>
<remarks>
The threads are real: what is faked is the inhibitor and the nudge. A test
starts one, waits for the recorder to show the first effect, stops it and
checks the release followed. The waits are bounded so a broken thread fails
the test rather than hanging the suite.
</remarks>
"""

import time

from deckplate import awake
from deckplate.holds import HoldManager, task_key


class FakeInhibitor:
    """<summary>Hands out cookies and remembers which were released.</summary>"""

    def __init__(self, fail=False):
        """<summary>Start empty; ``fail`` makes every inhibit raise.</summary>"""
        self.taken = []
        self.released = []
        self.fail = fail

    def inhibit(self, reason):
        """<summary>Record the reason and hand back the next cookie.</summary>"""
        if self.fail:
            raise RuntimeError("no service")
        self.taken.append(reason)
        return len(self.taken)

    def release(self, cookie):
        """<summary>Record the cookie given back.</summary>"""
        self.released.append(cookie)


def wait_for(condition, seconds=2.0):
    """<summary>Spin until a condition holds or the time is up.</summary>
    <returns>Whether it held.</returns>"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_stay_awake_takes_the_inhibit_nudges_and_releases_on_stop():
    """<summary>
    Starting Stay Awake inhibits and nudges at once; stopping it releases
    the cookie it took.
    </summary>
    """
    inhibitor = FakeInhibitor()
    nudges = []
    thread = awake.stay_awake({"inhibit": True, "nudge": True, "every_ms": 10},
                              inhibitor=inhibitor, nudge=lambda: nudges.append(1), log=lambda t: None)
    thread.start()
    assert wait_for(lambda: len(nudges) >= 2)
    assert inhibitor.taken == ["Stay Awake key on the deck"]
    thread.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert inhibitor.released == [1]


def test_stay_awake_without_either_half_ends_at_once():
    """<summary>
    With the inhibit off and the nudge off there is nothing to hold, so the
    thread finishes on its own and the key reads as off.
    </summary>
    """
    inhibitor = FakeInhibitor()
    thread = awake.stay_awake({"inhibit": False, "nudge": False, "every_ms": 10},
                              inhibitor=inhibitor, nudge=lambda: None, log=lambda t: None)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert inhibitor.taken == []


def test_a_failed_inhibit_still_nudges_and_is_logged_once():
    """<summary>
    On a desktop without the screensaver service the nudge is still worth
    having, so the thread carries on, and says once why the inhibit is not held.
    </summary>
    """
    lines = []
    nudges = []
    thread = awake.stay_awake({"inhibit": True, "nudge": True, "every_ms": 10},
                              inhibitor=FakeInhibitor(fail=True), nudge=lambda: nudges.append(1), log=lines.append)
    thread.start()
    assert wait_for(lambda: len(nudges) >= 1)
    thread.stop()
    thread.join(timeout=2)
    assert len([line for line in lines if "inhibit not taken" in line]) == 1


def test_do_not_disturb_holds_the_notification_inhibit_until_stopped():
    """<summary>Do Not Disturb takes the notification inhibit and releases it on stop.</summary>"""
    inhibitor = FakeInhibitor()
    thread = awake.do_not_disturb({}, inhibitor=inhibitor, log=lambda t: None)
    thread.start()
    assert wait_for(lambda: inhibitor.taken)
    assert thread.is_alive()
    thread.stop()
    thread.join(timeout=2)
    assert inhibitor.released == [1]


def test_the_hold_manager_toggles_a_desktop_task_under_its_key():
    """<summary>
    The manager starts a desktop toggle under its task key, reports it as
    running, and stops it on the second press or on stop_all.
    </summary>
    <remarks>This is the path a deck key takes: the runner asks the manager,
    and the controller reads <see cref="HoldManager.active_combos"/> to draw
    the key's active face, so the key here must match task_key's spelling.</remarks>"""
    manager = HoldManager(log=lambda t: None)
    inhibitor = FakeInhibitor()
    make = lambda: awake.do_not_disturb({}, inhibitor=inhibitor, log=lambda t: None)  # noqa: E731
    assert manager.toggle_task(task_key("dnd", {}), make, "do not disturb")
    assert "dnd" in manager.active_combos()
    assert wait_for(lambda: inhibitor.taken)
    assert not manager.toggle_task(task_key("dnd", {}), make, "do not disturb")
    assert "dnd" not in manager.active_combos()
    assert inhibitor.released == [1]
    assert manager.toggle_task(task_key("stay_awake", {}), lambda: awake.stay_awake(
        {"every_ms": 10}, inhibitor=inhibitor, nudge=lambda: None, log=lambda t: None), "stay awake")
    manager.stop_all()
    assert manager.active_combos() == set()
