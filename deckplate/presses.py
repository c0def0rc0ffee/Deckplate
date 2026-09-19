"""<summary>
Decide which of a key's three actions a press meant.

Turns the deck's press and release reports into the action the user meant:
the plain one, the long press one, or the double press one.
</summary>
<remarks>
The deck already reports a press and a release separately, so a long press
costs nothing but bookkeeping. That bookkeeping is all here, away from the
controller, because it is pure timing logic and is far easier to test with a
fake clock than with a fake deck.

Nothing in this module touches hardware, sends a keystroke or reads a config
file. It is handed the three actions belonging to a key and a monotonic time,
and it hands back a list of actions to run now, in order.

The rules, in the order they are applied:

* A key with neither a long nor a double action fires on the press, exactly as
  it always has. No waiting, no added latency, and the hold action keeps its
  press to toggle feel.
* A key that has a long action fires it the moment the hold threshold passes
  while the key is still down, not on release, so holding a key feels like it
  did something.
* Once the long action has fired, the release does nothing. The user has
  already had what they asked for.
* A key that has a double action cannot know on the first release whether a
  second press is coming, so the plain action waits out the double press
  window. That delay applies only to keys with a double action.
* A second press inside the window is the double press. It is not also a
  candidate for a long press: one gesture, one action.
</remarks>
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .config import Action

# Defaults in milliseconds. Both are settable per config in [deck]; these are
# the numbers used when it says nothing. 400 ms is long enough that a normal
# press never trips it and short enough that a deliberate hold feels prompt.
DEFAULT_LONG_MS = 400
DEFAULT_DOUBLE_MS = 300


@dataclass
class Actions:
    """<summary>
    The three actions bound to one key.
    </summary>
    <param name="plain">What a normal press does. None for a key that only has
    a second action bound, which is odd but legal.</param>
    <param name="long">What holding the key past the threshold does.</param>
    <param name="double">What two presses inside the window do.</param>
    """

    plain: Action | None = None
    long: Action | None = None
    double: Action | None = None

    @property
    def second(self) -> bool:
        """<summary>
        Whether this key's press has to be timed rather than acted on at once.
        </summary>
        <returns>True when the key has a long or a double action, so a press has to be timed.</returns>
        <remarks>
        This is the switch between the two behaviours in the whole module. A
        key where it is False is dispatched on the press with no latency at
        all, and one where it is True goes through the held and waiting state.
        </remarks>"""
        return self.long is not None or self.double is not None

    @property
    def any(self) -> bool:
        """<summary>
        Whether the key does anything when pressed.
        </summary>
        <returns>True when the key has anything at all bound to it.</returns>
        <remarks>
        False means an empty key, which is still drawn but does nothing when
        pressed, so the controller can tell an unbound key from a bound one
        without looking at all three fields itself.
        </remarks>"""
        return self.plain is not None or self.second

    def all(self) -> Iterable[Action]:
        """<summary>
        Every action on this key, for callers that need to look at all of them.
        </summary>
        <returns>Every action bound to the key, skipping the ones not set.</returns>
        <remarks>
        Always in the order plain, long, double, so a caller wanting the most
        ordinary of them can take the first. Nothing in the daemon calls this
        today: it exists for a caller that has to inspect a whole key, such as
        a check over a loaded config.
        </remarks>"""
        return [a for a in (self.plain, self.long, self.double) if a is not None]


@dataclass
class _Held:
    """<summary>
    A key that is down and whose gesture is not settled yet.
    </summary>
    <remarks>
    Holds its own copy of the actions, taken when the key went down, so a page
    change while the key is held cannot alter what the release means.
    ``long_fired`` is what makes the release do nothing afterwards.
    </remarks>"""

    actions: Actions
    pressed_at: float
    long_fired: bool = False


@dataclass
class _Waiting:
    """<summary>
    A released key whose plain action is waiting to see if a second press comes.
    </summary>
    <remarks>
    ``until`` is an absolute monotonic time, not a duration, so it can be
    compared straight against the clock the router is handed. The action may be
    None, for a key that has a double action and no plain one: the wait still
    happens, and nothing is run when it ends.
    </remarks>"""

    action: Action | None
    until: float


@dataclass
class PressRouter:
    """<summary>
    Timing for every key on the deck.
    </summary>
    <remarks>
    One router serves the whole deck: the state is keyed by (row, column), so
    two keys can be part way through a gesture at once without interfering.

    <see cref="due"/> is what keeps the daemon's loop honest. The controller
    normally blocks on the deck for up to a quarter of a second, which would
    make a 400 ms long press fire up to 250 ms late. Asking the router how
    long it may sleep keeps the threshold accurate without spinning when
    nothing is pending.

    There is no locking here at all. The router is meant to be driven from the
    one loop that reads the deck, and every method mutates the state, so
    calling it from a second thread would corrupt a gesture in flight.
    </remarks>
    """

    long_ms: int = DEFAULT_LONG_MS
    double_ms: int = DEFAULT_DOUBLE_MS
    _held: dict[tuple[int, int], _Held] = field(default_factory=dict)
    _waiting: dict[tuple[int, int], _Waiting] = field(default_factory=dict)

    @property
    def pending(self) -> bool:
        """<summary>
        Whether anything is part way through a gesture.
        </summary>
        <returns>True while any key is mid gesture and the loop must keep ticking.</returns>
        <remarks>
        True does not mean something is due now, only that something will be.
        Ask <see cref="due"/> for how long the loop may wait.
        </remarks>"""
        return bool(self._held or self._waiting)

    def clear(self) -> None:
        """<summary>
        Forget every gesture in flight.
        </summary>
        <remarks>
        Called when the page changes or the config is reloaded. The actions
        held here belong to the page that was showing when the key went down,
        and running one of those against a page that has since gone would do
        something the user did not ask for. Dropping them is the safe answer.
        </remarks>
        """
        self._held.clear()
        self._waiting.clear()

    def press(self, row: int, column: int, actions: Actions, now: float) -> list[Action]:
        """<summary>
        Take a press report and say what, if anything, runs now.
        </summary>
        <param name="row">Key row on the page that is showing.</param>
        <param name="column">Key column on the page that is showing.</param>
        <param name="actions">The three actions bound to the key, read from the
        page that is showing at this moment. They are kept for the length of the
        gesture, so a page switch part way through cannot change what a release
        means.</param>
        <param name="now">A monotonic time in seconds.</param>
        <returns>The actions to run immediately, which is usually none.</returns>
        <remarks>
        A key with neither a long nor a double action returns its plain action
        straight away, which is why an ordinary key has no added latency.
        Everything else records state and returns nothing, and the action
        arrives later from <see cref="release"/> or <see cref="tick"/>.

        A press for a key already recorded as held overwrites the record. That
        happens when a release report is lost, and losing the earlier gesture
        is better than leaving a key held forever.
        </remarks>
        """
        position = (row, column)
        waiting = self._waiting.pop(position, None)
        if waiting is not None and actions.double is not None:
            # Second press inside the window. The plain action that was waiting
            # is dropped: the user asked for the double, not for both. Nothing
            # is recorded as held, so this press cannot also become a long one.
            return [actions.double]
        if not actions.second:
            return [actions.plain] if actions.plain is not None else []
        if waiting is not None and waiting.action is not None:
            # A second press on a key with a long action but no double action.
            # The first press is still owed its plain action, so run it now
            # rather than swallowing it, and start the new gesture cleanly.
            self._held[position] = _Held(actions, now)
            return [waiting.action]
        self._held[position] = _Held(actions, now)
        return []

    def release(self, row: int, column: int, now: float) -> list[Action]:
        """<summary>
        Take a release report and settle the gesture it ends.
        </summary>
        <param name="row">Key row, matching the press.</param>
        <param name="column">Key column, matching the press.</param>
        <param name="now">A monotonic time in seconds, from the same clock as
        the press.</param>
        <returns>The actions to run immediately, which is the plain one when the
        press was short and the key has no double action to wait for.</returns>
        <remarks>
        A release for a key with nothing recorded returns nothing rather than
        raising. That is the normal case after <see cref="clear"/> and after a
        long press has already fired, so it must stay quiet.

        The actions used are the ones kept from the press, not the ones on the
        page now, which is what makes a page switch mid gesture safe.
        </remarks>
        """
        position = (row, column)
        held = self._held.pop(position, None)
        if held is None or held.long_fired:
            return []
        if held.actions.double is not None:
            self._waiting[position] = _Waiting(held.actions.plain, now + self.double_ms / 1000)
            return []
        return [held.actions.plain] if held.actions.plain is not None else []

    def tick(self, now: float) -> list[Action]:
        """<summary>
        Fire anything whose time has come.
        </summary>
        <param name="now">A monotonic time in seconds.</param>
        <returns>The long press actions whose threshold has just passed, then
        any plain actions whose double press window has closed unanswered.</returns>
        <remarks>
        Must be called regularly while <see cref="pending"/> is True, because
        nothing else moves a gesture on: a long press that is never ticked
        never fires. The controller calls it every time round its loop.

        A held key is marked as fired rather than removed, so it stays known
        until its release comes in and that release then does nothing. Waiting
        entries are removed as they expire.

        Calling it with a time that has gone backwards simply fires nothing,
        which is why a monotonic clock is asked for rather than a wall clock.
        </remarks>
        """
        due: list[Action] = []
        for held in self._held.values():
            if held.long_fired or held.actions.long is None:
                continue
            if (now - held.pressed_at) * 1000 >= self.long_ms:
                held.long_fired = True
                due.append(held.actions.long)
        for position in [p for p, w in self._waiting.items() if now >= w.until]:
            waiting = self._waiting.pop(position)
            if waiting.action is not None:
                due.append(waiting.action)
        return due

    def due(self, now: float) -> float | None:
        """<summary>
        How long until the next thing is due.
        </summary>
        <param name="now">A monotonic time in seconds.</param>
        <returns>Seconds until the soonest pending deadline, never below zero,
        or None when nothing is pending and the loop may sleep as it likes.</returns>
        <remarks>
        This is how the long press threshold stays accurate without the loop
        spinning. The controller uses it as the upper bound on how long it will
        block reading the deck, so a shorter answer costs a wasted wake up and
        a longer one would fire the action late.

        Zero means due now and is different from None, which means nothing is
        waiting. Treating the two the same turns an idle deck into a busy loop.
        </remarks>
        """
        deadlines: list[float] = []
        for held in self._held.values():
            if not held.long_fired and held.actions.long is not None:
                deadlines.append(held.pressed_at + self.long_ms / 1000)
        deadlines.extend(w.until for w in self._waiting.values())
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)
