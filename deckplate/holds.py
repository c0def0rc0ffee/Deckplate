"""<summary>
Hold a keyboard key down until told to stop, letting go now and then.
</summary>
<remarks>
For games that want a key held. One deck press starts the hold, the next
stops it. While holding, the key is released for a random time between two
limits and then pressed again, after a random hold time between two other
limits, so it does not look like a key taped down. Set the release range to
zero for a plain hold.

Each hold runs on its own thread. The manager is keyed by the combination,
so pressing any deck key bound to the same combination toggles the same hold.

Three shapes of task live here and all three follow the same contract: a
daemon thread, a stop event rather than any kind of kill, and a finally block
that releases whatever it is holding. A key left down when a thread dies is
the worst failure this module has, because the keyboard stays stuck with
nothing on screen to explain it, so every exit path releases.

The threads are daemon threads, so an interpreter that exits without calling
<see cref="HoldManager.stop_all"/> will drop them mid cycle with keys still
down. The controller calls stop_all on shutdown for exactly that reason.
</remarks>
"""

from __future__ import annotations

import random
import threading
from typing import Callable

from . import hotkeys


class Hold(threading.Thread):
    """<summary>
    One key combination held down, with an occasional deliberate let go.
    </summary>
    <remarks>
    Start it with <see cref="threading.Thread.start"/> and end it with
    <see cref="stop"/> followed by a join. It cannot be restarted afterwards,
    which is why the manager builds a fresh one each time rather than keeping
    one per combination.

    The cycle is press, wait a random hold time, release, wait a random release
    time, press again. A release span whose upper bound is zero turns that into
    a plain unbroken hold: the thread still wakes on the hold timer, sees there
    is nothing to do and goes straight back to waiting, which is how stop is
    still noticed promptly.

    <see cref="cycles"/> counts completed let go and press again pairs and is
    read by the configuration page for its running indicator. It is written
    without a lock because a slightly stale count on screen does not matter.
    </remarks>
    """

    def __init__(self, combo: str, hold_ms: tuple[int, int], release_ms: tuple[int, int], *,
                 press: Callable[[str], None] = hotkeys.press,
                 release: Callable[[str], None] = hotkeys.release,
                 rng: Callable[[float, float], float] = random.uniform,
                 log: Callable[[str], None] = print) -> None:
        """<summary>
        Set up a hold thread. Nothing is pressed until it is started.
        </summary>
        <param name="combo">The combination to hold, in the spelling
        <see cref="hotkeys.parse_combo"/> accepts.</param>
        <param name="hold_ms">Lower and upper bounds, in milliseconds, for how
        long the key stays down before a let go.</param>
        <param name="release_ms">Lower and upper bounds for how long the key
        stays up. An upper bound of zero means never let go at all.</param>
        <param name="log">Where a backend failure is reported.</param>
        <remarks>
        The bounds are handed to the random source as they are, so a lower
        bound above the upper one is not rejected here. The config layer is
        what keeps them the right way round.

        The thread name carries the combination, which is what makes a stuck
        hold identifiable in a stack dump.
        </remarks>
        """
        super().__init__(name=f"hold:{combo}", daemon=True)
        self.combo = combo
        self.hold_ms = hold_ms
        self.release_ms = release_ms
        self.press = press
        self.release = release
        self.rng = rng
        self.log = log
        # Not named _stop: threading.Thread has an internal _stop() method that
        # join() and is_alive() call on Python 3.12 and older.
        self._stop_event = threading.Event()
        self.cycles = 0

    def stop(self) -> None:
        """<summary>
        Ask the hold to finish at its next wake up.
        </summary>
        <remarks>
        Returns immediately and does not wait: the key may still be down when
        this call comes back. Join the thread if that matters. The key is
        released by the thread itself on the way out, never by the caller.
        </remarks>
        """
        self._stop_event.set()

    def _wait(self, span: tuple[int, int]) -> bool:
        """<summary>
        Sleep a random time in the span. True if stop was requested meanwhile.
        </summary>
        <param name="span">Lower and upper bounds in milliseconds.</param>
        <returns>True when stop was asked for during the wait, which means the
        caller should give up rather than carry on to the next step.</returns>
        <remarks>
        Waiting on the stop event rather than sleeping is what makes a stop
        prompt: a plain sleep would leave the key down for the rest of the
        span. An upper bound of zero waits zero seconds, which still polls the
        event and so still reports a stop.
        </remarks>
        """
        low, high = span
        seconds = self.rng(low, high) / 1000 if high > 0 else 0
        return self._stop_event.wait(seconds)

    def run(self) -> None:
        """<summary>
        The thread body: press, cycle until stopped, and always let go.
        </summary>
        <remarks>
        Do not call this directly. Use start, or the key goes down on the
        caller's own thread and blocks it until stop is asked for.

        ``down`` tracks whether the key is believed to be held, so the finally
        block releases once and only once. A release sent for a key that is
        already up is harmless on both backends, but a missed release is not,
        so the flag errs towards releasing.

        Any exception at all is caught and logged rather than raised. A lost
        display or a backend that has gone away is a normal end to a session,
        and a traceback from a daemon thread would be printed with no context
        and no way to act on it.
        </remarks>
        """
        down = False
        try:
            self.press(self.combo)
            down = True
            while not self._wait(self.hold_ms):
                if self.release_ms[1] <= 0:
                    continue
                self.release(self.combo)
                down = False
                if self._wait(self.release_ms):
                    break
                self.press(self.combo)
                down = True
                self.cycles += 1
        except Exception as err:  # a lost display or backend: log and give up cleanly
            self.log(f"hold {self.combo} stopped: {err}")
        finally:
            if down:
                try:
                    self.release(self.combo)
                except Exception as err:
                    self.log(f"could not release {self.combo}: {err}")


class Boost(threading.Thread):
    """<summary>
    Hold one key down while a second is pressed and let go, over and over.
    </summary>
    <remarks>
    Forward and then boosting: the hold combo stays down for the whole run, and
    the boost combo is pressed for ``on_ms`` and released for ``off_ms`` until
    it is stopped. Both keys are let go on the way out, whatever went wrong.

    Unlike <see cref="Hold"/> the timings here are fixed, not random: this one
    is about a steady rhythm rather than looking like a person.

    Either combination may be empty. An empty boost leaves the thread simply
    holding the first key, waking every quarter second only so a stop is
    noticed, and an empty hold gives a bare on and off tapper. Both empty is
    refused by the caller rather than here.

    The release order in the finally block matters: the boost key is let go
    before the hold key, matching the order they went down, so nothing is left
    looking like a modifier held over a released key.
    </remarks>
    """

    def __init__(self, hold_combo: str, boost_combo: str, on_ms: int, off_ms: int, *,
                 press: Callable[[str], None] = hotkeys.press,
                 release: Callable[[str], None] = hotkeys.release,
                 log: Callable[[str], None] = print) -> None:
        """<summary>
        Set up a boost thread. Nothing is pressed until it is started.
        </summary>
        <param name="hold_combo">The combination held for the whole run. May be
        empty for a plain tapper.</param>
        <param name="boost_combo">The combination tapped on and off. May be
        empty to hold only.</param>
        <param name="on_ms">How long the boost key stays down each cycle.</param>
        <param name="off_ms">How long it stays up between cycles.</param>
        <param name="log">Where a backend failure is reported.</param>
        <remarks>
        A zero ``on_ms`` or ``off_ms`` is legal and gives a tight loop with no
        wait, which will flood the backend. The config layer is what keeps the
        numbers sensible.
        </remarks>
        """
        super().__init__(name=f"boost:{hold_combo}+{boost_combo}", daemon=True)
        self.hold_combo = hold_combo
        self.boost_combo = boost_combo
        self.on_ms = on_ms
        self.off_ms = off_ms
        self.press = press
        self.release = release
        self.log = log
        self._stop_event = threading.Event()
        self.cycles = 0

    def stop(self) -> None:
        """<summary>
        Ask the boost to finish at its next wake up.
        </summary>
        <remarks>
        Returns at once. Both keys are released by the thread itself, so a
        caller that needs them up before carrying on has to join.
        </remarks>
        """
        self._stop_event.set()

    def run(self) -> None:
        """<summary>
        The thread body: take the hold down, cycle the boost, release both.
        </summary>
        <remarks>
        Do not call this directly. Use start.

        ``holding`` and ``boosting`` track what is believed to be down so the
        finally block releases exactly what it should, in the order the keys
        went down. Every wait is on the stop event rather than a sleep, so a
        stop is acted on within one cycle instead of at the end of it.

        Exceptions are caught and logged, not raised, for the same reason as in
        <see cref="Hold.run"/>: a backend that has gone away ends the session
        normally rather than dumping a traceback from a daemon thread.
        </remarks>
        """
        holding = False
        boosting = False
        try:
            if self.hold_combo:
                self.press(self.hold_combo)
                holding = True
            while not self._stop_event.is_set():
                if not self.boost_combo:
                    # Nothing to boost with: just keep the hold down.
                    if self._stop_event.wait(0.25):
                        break
                    continue
                self.press(self.boost_combo)
                boosting = True
                if self._stop_event.wait(self.on_ms / 1000):
                    break
                self.release(self.boost_combo)
                boosting = False
                self.cycles += 1
                if self._stop_event.wait(self.off_ms / 1000):
                    break
        except Exception as err:  # a lost display or backend: log and give up cleanly
            self.log(f"boost {self.hold_combo}+{self.boost_combo} stopped: {err}")
        finally:
            for down, combo in ((boosting, self.boost_combo), (holding, self.hold_combo)):
                if down:
                    try:
                        self.release(combo)
                    except Exception as err:
                        self.log(f"could not release {combo}: {err}")


class Repeat(threading.Thread):
    """<summary>
    Tap a key, wait, tap it again, until it is stopped.
    </summary>
    <remarks>
    The simplest of the three: nothing is ever left down, so there is no
    release to guarantee and no flag to track. A failure part way through
    leaves the keyboard exactly as it was.

    The wait is the gap between taps, not the period of the cycle, so the real
    rate is a little slower than ``every_ms`` by however long the backend takes
    to send each tap.

    An empty combination makes the thread a timer that presses nothing, which
    is the half filled in binding case and is left running rather than treated
    as an error.
    </remarks>
    """

    def __init__(self, combo: str, every_ms: int, *,
                 send: Callable[[str], None] = hotkeys.send,
                 log: Callable[[str], None] = print) -> None:
        """<summary>
        Set up a repeat thread. Nothing is sent until it is started.
        </summary>
        <param name="combo">The combination to tap. May be empty.</param>
        <param name="every_ms">The gap between taps in milliseconds. Zero gives
        a tight loop with no wait at all.</param>
        <param name="log">Where a backend failure is reported.</param>
        """
        super().__init__(name=f"repeat:{combo}", daemon=True)
        self.combo = combo
        self.every_ms = every_ms
        self.send = send
        self.log = log
        self._stop_event = threading.Event()
        self.presses = 0

    def stop(self) -> None:
        """<summary>
        Ask the repeat to finish at its next wake up.
        </summary>
        <remarks>
        Returns at once. One more tap may still be sent before the thread sees
        the event, which is harmless because a tap is complete in itself.
        </remarks>
        """
        self._stop_event.set()

    def run(self) -> None:
        """<summary>
        The thread body: tap and wait until stopped.
        </summary>
        <remarks>
        Do not call this directly. Use start.

        The stop is checked at the top of the loop and again during the wait,
        so a stop asked for mid gap ends the thread without another tap.
        Exceptions are caught and logged rather than raised, as in the other
        two thread bodies here.
        </remarks>
        """
        try:
            while not self._stop_event.is_set():
                if self.combo:
                    self.send(self.combo)
                    self.presses += 1
                if self._stop_event.wait(self.every_ms / 1000):
                    break
        except Exception as err:
            self.log(f"repeat {self.combo} stopped: {err}")


def task_key(action_type: str, params: dict) -> str:
    """<summary>
    The manager key for a toggling action.
    </summary>
    <param name="action_type">One of hold, boost or repeat. Anything else gives
    an empty string, because no other action type has a running state.</param>
    <param name="params">That action's params, read leniently so a partly
    filled in binding still produces a stable key.</param>
    <returns>The string the manager files the task under.</returns>
    <remarks>
    One key per thing being toggled, so any deck key bound to the same thing
    switches the same task off, and the page can mark it as running.

    A hold is filed under the bare combination with no prefix, which is
    deliberate and load bearing: a hold is toggled by combination alone, and
    the boost and repeat prefixes are what stop those colliding with a hold on
    the same keys.

    The controller calls this against the set from
    <see cref="HoldManager.active_combos"/> to decide which keys to draw with a
    running border, so the spelling here and what the manager files a task
    under have to stay the same string.
    </remarks>
    """
    if action_type == "hold":
        return params.get("keys", "")
    if action_type == "boost":
        return f"boost:{params.get('hold', '')}+{params.get('keys', '')}"
    if action_type == "repeat":
        return f"repeat:{params.get('keys', '')}"
    return ""


class HoldManager:
    """<summary>
    Owns every running hold, boost and repeat, one per key from
    <see cref="task_key"/>.
    </summary>
    <remarks>
    There is one manager for the whole daemon, living on the action runner, so
    a task started from one page can be stopped from another or from a second
    deck key bound to the same thing. Building a second manager would give two
    tasks that neither knows about the other, both pressing the same keys.

    Every method takes the lock, and the dictionary only ever holds the task
    currently filed under a key. A finished thread is left in place until
    something asks about that key again, so the dictionary can contain dead
    threads: that is why every read tests <see cref="threading.Thread.is_alive"/>
    rather than trusting membership.

    Stopping joins with a two second timeout and then gives up. A backend that
    has hung will leave the thread behind rather than blocking the deck's loop,
    and the key it was holding may stay down until the session ends.
    </remarks>
    """

    def __init__(self, *, press=hotkeys.press, release=hotkeys.release,
                 send=hotkeys.send, rng=random.uniform,
                 log: Callable[[str], None] = print) -> None:
        """<summary>
        Build an empty manager, holding the backend calls its tasks will use.
        </summary>
        <param name="press">How a task pushes keys down.</param>
        <param name="release">How a task lets them go.</param>
        <param name="send">How a repeat taps a combination.</param>
        <param name="rng">The random source for a hold's timings, taking a low
        and a high bound and returning a value between them.</param>
        <param name="log">Where started, stopped and failed messages go.</param>
        <remarks>
        These are passed down to each task as it is made, not used here, which
        is what lets a test start real threads that press nothing real.
        </remarks>
        """
        self._holds: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self.press = press
        self.release = release
        self.send = send
        self.rng = rng
        self.log = log

    def active(self, combo: str) -> bool:
        """<summary>
        Is a hold running for this combination right now.
        </summary>
        <param name="combo">A plain combination, which is also a hold's task
        key. A boost or a repeat is filed under a prefixed key and will not be
        found by this.</param>
        <returns>True when a live thread is filed under that combination.</returns>
        <remarks>
        The answer is stale the moment it is returned: the task may finish on
        its own thread immediately afterwards. It is meant for drawing the
        running state on a key, not for deciding whether to stop something.
        </remarks>
        """
        with self._lock:
            hold = self._holds.get(combo)
            return hold is not None and hold.is_alive()

    def active_combos(self) -> set[str]:
        """<summary>
        The task keys of everything running, holds, boosts and repeats alike.
        </summary>
        <returns>A fresh set, safe to keep and iterate after the lock is let
        go. Dead threads are filtered out.</returns>
        <remarks>
        The controller takes this once per redraw and tests each key's
        <see cref="task_key"/> against it, which is why the set is of task keys
        and not of bare combinations.
        </remarks>
        """
        with self._lock:
            return {combo for combo, hold in self._holds.items() if hold.is_alive()}

    def _toggle(self, key: str, make: Callable[[], threading.Thread],
                started: str, stopped: str) -> bool:
        """<summary>
        Start the task under ``key``, or stop it if it is already running.
        </summary>
        <param name="key">The task key from <see cref="task_key"/>.</param>
        <param name="make">Builds the thread, called only when one is about to
        be started. It runs while the lock is held, so it must not block.</param>
        <param name="started">The line to log when one is started.</param>
        <param name="stopped">The line to log when one is stopped.</param>
        <returns>True when it is now running.</returns>
        <remarks>
        The whole toggle happens under the lock, including the join, so two
        presses arriving together cannot both decide to start. That also means
        a stop can hold the lock for the full two second join timeout and every
        other call waits behind it, which is accepted: the alternative is a
        window in which a second press starts a duplicate task.

        The entry is popped before anything else, so a dead thread left in the
        dictionary is replaced rather than mistaken for a running one.
        </remarks>
        """
        with self._lock:
            task = self._holds.pop(key, None)
            if task is not None and task.is_alive():
                task.stop()
                task.join(timeout=2)
                self.log(stopped)
                return False
            task = make()
            self._holds[key] = task
            task.start()
            self.log(started)
            return True

    def toggle(self, combo: str, hold_ms: tuple[int, int], release_ms: tuple[int, int]) -> bool:
        """<summary>
        Start the hold, or stop it if it is running.
        </summary>
        <param name="combo">The combination to hold, which is also its task key.</param>
        <param name="hold_ms">Lower and upper bounds for how long the key stays down.</param>
        <param name="release_ms">Lower and upper bounds for the let go. An upper
        bound of zero gives an unbroken hold.</param>
        <returns>True when the key is now being held.</returns>
        <remarks>
        Filed under the bare combination, so any deck key bound to the same
        combination toggles this same hold even with different timings. The
        timings of the task already running win: the second press stops it and
        never reaches the new values.
        </remarks>
        """
        return self._toggle(
            combo,
            lambda: Hold(combo, hold_ms, release_ms, press=self.press, release=self.release,
                         rng=self.rng, log=self.log),
            f"holding {combo}", f"released {combo}")

    def toggle_boost(self, hold_combo: str, boost_combo: str, on_ms: int, off_ms: int) -> bool:
        """<summary>
        Start forward and boosting, or stop it.
        </summary>
        <param name="hold_combo">Held for the whole run.</param>
        <param name="boost_combo">Tapped on and off against it.</param>
        <param name="on_ms">How long the boost key stays down each cycle.</param>
        <param name="off_ms">How long it stays up between cycles.</param>
        <returns>True when it is now running.</returns>
        <remarks>
        Filed under a key built from both combinations, so the same pair
        toggles the same task and a different pairing of the same keys is a
        separate one. A hold on the same key as ``hold_combo`` is independent
        of this and can be running at the same time, which will leave the key
        down after this task releases it.
        </remarks>
        """
        key = task_key("boost", {"hold": hold_combo, "keys": boost_combo})
        return self._toggle(
            key,
            lambda: Boost(hold_combo, boost_combo, on_ms, off_ms,
                          press=self.press, release=self.release, log=self.log),
            f"holding {hold_combo} and boosting {boost_combo}",
            f"released {hold_combo} and {boost_combo}")

    def toggle_repeat(self, combo: str, every_ms: int) -> bool:
        """<summary>
        Start tapping the key on a timer, or stop it.
        </summary>
        <param name="combo">The combination to tap.</param>
        <param name="every_ms">The gap between taps in milliseconds.</param>
        <returns>True when it is now running.</returns>
        <remarks>
        Filed under a repeat prefixed key, so tapping a combination and holding
        the same combination are two separate tasks that can run at once.
        </remarks>
        """
        key = task_key("repeat", {"keys": combo})
        return self._toggle(
            key,
            lambda: Repeat(combo, every_ms, send=self.send, log=self.log),
            f"repeating {combo} every {every_ms} ms", f"stopped repeating {combo}")

    def stop_all(self) -> None:
        """<summary>
        Stop every task and wait for the keys to come back up.
        </summary>
        <remarks>
        Call this on shutdown, on a config reload and on losing the deck.
        Skipping it leaves daemon threads holding keys down with nothing on
        screen to say so, and on an interpreter exit they are killed mid cycle
        and never release.

        The dictionary is emptied and copied under the lock, then every task is
        asked to stop before any of them is joined. Stopping all first means
        the joins overlap, so the whole call takes about two seconds at worst
        rather than two seconds for each task.

        A task that does not finish inside its join timeout is abandoned. This
        method never raises and never reports which ones were left behind.
        </remarks>
        """
        with self._lock:
            holds = list(self._holds.values())
            self._holds.clear()
        for hold in holds:
            hold.stop()
        for hold in holds:
            hold.join(timeout=2)
