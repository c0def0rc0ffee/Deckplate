"""<summary>
What a key remembers between presses: a toggle's state, a counter's count,
a timer's clock, a stopwatch's clock.
</summary>
<remarks>
Four action types act on the key they sit on rather than on the PC, and
their whole meaning is the state kept here. The state lives in the
controller for as long as the daemon runs and is keyed by page name and
position, so switching pages and reloading the config leave it alone, and a
restart clears it.

Everything is worked out from a monotonic clock passed in by the caller,
never read here, so a timer can be tested in microseconds and cannot be
thrown by the wall clock changing. Nothing here draws: the face a state
wants is described as text and a ring fraction, and the tiles module draws
that.
</remarks>
"""

from __future__ import annotations

from dataclasses import dataclass, field

# How long a finished timer shows 0:00 with its full red ring before going
# back to its idle face.
FINISHED_SECONDS = 10.0
# The stopwatch's ring completes once a minute, like a second hand.
STOPWATCH_RING_SECONDS = 60.0

Position = tuple[str, int, int]


def format_seconds(seconds: float) -> str:
    """<summary>
    Seconds as ``m:ss``, or ``h:mm:ss`` from an hour up.
    </summary>
    <param name="seconds">Any non negative number. Fractions are cut, not rounded.</param>
    <returns>The text a key shows.</returns>
    <remarks>
    Cutting rather than rounding is what makes a countdown read as
    expected: 4.9 seconds left is "0:04", and "0:00" appears only when the
    time is really up. For a stopwatch the same rule means the digit changes
    exactly when the second completes.
    </remarks>
    """
    whole = max(0, int(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@dataclass(frozen=True)
class Face:
    """<summary>
    What a live key should show right now.
    </summary>
    <param name="text">The big text on the key, such as "4:59" or "12".</param>
    <param name="ring">How much of the ring around the edge to draw, 0 to 1,
    or None for no ring.</param>
    <param name="alert">True while a timer has just finished, which draws the
    ring in the alert colour rather than the running one.</param>
    <param name="active">True while the thing is running or on, which is what
    picks the key's active picture and label.</param>
    <remarks>
    A frozen value so it can be compared: the controller redraws a key only
    when its face differs from the last one drawn, and that comparison is
    the whole reason this is not a bag of loose values.
    </remarks>
    """

    text: str | None = None
    ring: float | None = None
    alert: bool = False
    active: bool = False


@dataclass
class ToggleState:
    """<summary>A toggle key: on or off.</summary>
    <param name="on">True after an odd number of presses.</param>"""

    on: bool = False


@dataclass
class CounterState:
    """<summary>A counter key: the running count.</summary>
    <param name="count">Starts at zero and moves by the action's step.</param>"""

    count: int = 0


@dataclass
class ClockState:
    """<summary>
    A timer or a stopwatch: time that accumulates while running.
    </summary>
    <param name="total">The countdown length in seconds, or None for a stopwatch.</param>
    <param name="running">Whether the clock is going now.</param>
    <param name="started">The monotonic time the current run began, while running.</param>
    <param name="banked">Seconds accumulated by earlier runs, before the current one.</param>
    <param name="finished_at">When a timer reached zero, while it is showing that.</param>
    <remarks>
    Elapsed time is the banked seconds plus the current run, so pausing and
    resuming is a matter of moving the current run into the bank. A timer
    and a stopwatch differ only in whether ``total`` is set, and in how the
    face reads the same elapsed time.
    </remarks>
    """

    total: float | None = None
    running: bool = False
    started: float | None = None
    banked: float = 0.0
    finished_at: float | None = None

    def elapsed(self, now: float) -> float:
        """<summary>Seconds run so far, including the current run.</summary>
        <param name="now">The monotonic time.</param>
        <returns>A non negative number.</returns>"""
        if self.running and self.started is not None:
            return self.banked + max(0.0, now - self.started)
        return self.banked

    def remaining(self, now: float) -> float | None:
        """<summary>Seconds left on a timer, or None for a stopwatch.</summary>
        <param name="now">The monotonic time.</param>
        <returns>Never below zero.</returns>"""
        if self.total is None:
            return None
        return max(0.0, self.total - self.elapsed(now))

    def press(self, now: float) -> None:
        """<summary>Start when stopped, pause when running; a finished timer starts afresh.</summary>
        <param name="now">The monotonic time.</param>"""
        if self.finished_at is not None:
            self.reset()
        if self.running:
            self.banked = self.elapsed(now)
            self.running = False
            self.started = None
        else:
            self.running = True
            self.started = now

    def reset(self) -> None:
        """<summary>Back to the start, stopped.</summary>"""
        self.running = False
        self.started = None
        self.banked = 0.0
        self.finished_at = None

    def settle(self, now: float) -> bool:
        """<summary>
        Move a timer that has run out into its finished state, once.
        </summary>
        <param name="now">The monotonic time.</param>
        <returns>True the one time the timer is found to have just finished,
        so the caller can run the done action.</returns>
        <remarks>
        Called on every tick. After FINISHED_SECONDS in the finished state the
        timer resets itself so the key goes back to showing its length, and
        that later transition returns False: the done action runs once.
        </remarks>
        """
        if self.total is None:
            return False
        if self.finished_at is not None:
            if now - self.finished_at >= FINISHED_SECONDS:
                self.reset()
            return False
        if self.running and self.remaining(now) == 0.0:
            self.running = False
            self.started = None
            self.banked = self.total
            self.finished_at = now
            return True
        return False

    def face(self, now: float) -> Face:
        """<summary>What the key shows for this clock right now.</summary>
        <param name="now">The monotonic time.</param>
        <returns>A Face: the time as text, the ring, and whether it is active.</returns>
        <remarks>
        A timer's ring empties as it counts down and is full and red while it
        shows finished. An idle timer shows its whole length with a full
        ring, so the key reads as "5:00" before it is ever pressed. A
        stopwatch's ring sweeps round once a minute and its idle face is
        0:00 with no ring.
        </remarks>"""
        if self.total is not None:
            if self.finished_at is not None:
                return Face(text="0:00", ring=1.0, alert=True, active=False)
            left = self.remaining(now) or 0.0
            return Face(text=format_seconds(left), ring=left / self.total if self.total else 0.0,
                        active=self.running)
        elapsed = self.elapsed(now)
        ring = (elapsed % STOPWATCH_RING_SECONDS) / STOPWATCH_RING_SECONDS if elapsed > 0 else None
        return Face(text=format_seconds(elapsed), ring=ring, active=self.running)


class KeyStates:
    """<summary>
    Every live key's state, keyed by page name and position.
    </summary>
    <remarks>
    A plain dictionary with the four kinds of state behind typed accessors,
    so a toggle can never be read as a counter. States are created on first
    use and never dropped, which is fine: there are at most fifteen keys a
    page and the daemon runs for days, not years.

    Only the controller's loop thread touches this. It is never read from
    the server threads, which see the results through the tiles instead.
    </remarks>
    """

    def __init__(self) -> None:
        """<summary>Start with nothing remembered.</summary>"""
        self._states: dict[Position, object] = {}

    def _get(self, key: Position, kind: type):
        """<summary>The state of one kind at a position, made if absent.</summary>
        <param name="key">Page name, row, column.</param>
        <param name="kind">The state class expected there.</param>
        <returns>The state, always of that kind.</returns>
        <remarks>A position whose action type changed between two presses
        gets a fresh state of the new kind rather than a wrong one.</remarks>"""
        state = self._states.get(key)
        if not isinstance(state, kind):
            state = kind()
            self._states[key] = state
        return state

    def peek(self, key: Position):
        """<summary>The state at a position, or None if the key has never been pressed.</summary>
        <param name="key">Page name, row, column.</param>
        <returns>Whatever state is there, or None.</returns>"""
        return self._states.get(key)

    def toggle(self, key: Position) -> bool:
        """<summary>Flip a toggle and say what it is now.</summary>
        <param name="key">Page name, row, column.</param>
        <returns>True when the toggle is now on.</returns>"""
        state = self._get(key, ToggleState)
        state.on = not state.on
        return state.on

    def is_on(self, key: Position) -> bool:
        """<summary>Whether a toggle is on, without changing it.</summary>
        <param name="key">Page name, row, column.</param>
        <returns>False for a toggle never pressed.</returns>"""
        state = self._states.get(key)
        return isinstance(state, ToggleState) and state.on

    def count(self, key: Position, step: int = 0, reset: bool = False) -> int:
        """<summary>Move a counter and say where it is.</summary>
        <param name="key">Page name, row, column.</param>
        <param name="step">How far to move, negative to go down. Zero reads it.</param>
        <param name="reset">True to go back to zero instead.</param>
        <returns>The count after the change.</returns>"""
        state = self._get(key, CounterState)
        if reset:
            state.count = 0
        else:
            state.count += step
        return state.count

    def clock(self, key: Position, total: float | None) -> ClockState:
        """<summary>The timer or stopwatch at a position, made or retuned as needed.</summary>
        <param name="key">Page name, row, column.</param>
        <param name="total">The timer length, or None for a stopwatch.</param>
        <returns>The clock state.</returns>
        <remarks>A timer whose configured length changed since it was last
        used is reset to the new length, since the old countdown would be
        counting towards the wrong zero.</remarks>"""
        state = self._get(key, ClockState)
        if state.total != total:
            state.reset()
            state.total = total
        return state

    def settle(self, now: float) -> list[Position]:
        """<summary>Let every timer notice it has run out.</summary>
        <param name="now">The monotonic time.</param>
        <returns>The positions whose timer just finished, for their done actions.</returns>"""
        finished = []
        for key, state in self._states.items():
            if isinstance(state, ClockState) and state.settle(now):
                finished.append(key)
        return finished

    def any_running(self, page: str) -> bool:
        """<summary>Whether any clock on a page is going or showing finished.</summary>
        <param name="page">The page name.</param>
        <returns>True when the page needs redrawing on a short clock.</returns>"""
        for (name, _row, _column), state in self._states.items():
            if name == page and isinstance(state, ClockState) and (state.running or state.finished_at is not None):
                return True
        return False
