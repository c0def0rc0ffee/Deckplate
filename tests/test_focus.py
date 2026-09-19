"""<summary>
Reading the focused window, and the watcher that polls it.

Nothing here runs xprop or touches a real desktop: the parsers are fed the
output they would have got, and the watcher is given a fake reader.
</summary>
<remarks>
Focus drives which page of the deck is showing, so every failure in this
file surfaces to the user as the wrong keys on the deck rather than as an
error. That is worth remembering when judging how loudly a fault here should
be reported: quiet and wrong is the danger, not noisy.

The sample text is real xprop output, kept exactly as the tool prints it,
punctuation and all. The suite must also pass on a build machine with no
display at all, which is why the environment is always faked rather than
read.
</remarks>
"""

import threading

from deckplate import focus
from deckplate.focus import FocusWatcher, Window

ACTIVE = "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3400007, 0x0\n"
PROPERTIES = (
    'WM_CLASS = "Navigator", "firefox"\n'
    '_NET_WM_NAME = "Deckplate on GitHub - Mozilla Firefox"\n'
    'WM_NAME = "Deckplate on GitHub - Mozilla Firefox"\n'
)


def test_the_active_window_id_is_read():
    """<summary>
    Pins the window id being lifted out of the root property line.
    </summary>
    <remarks>
    The line carries two comma separated values and only the first is the
    window; the second is a legacy field that is almost always zero. Taking
    the wrong one would ask for the properties of window zero and report no
    focus forever, so the deck would sit on its default page whatever the
    user did.
    </remarks>
    """
    assert focus.parse_active_window_id(ACTIVE) == "0x3400007"


def test_no_focused_window_reads_as_none():
    """<summary>
    Pins the three shapes of nothing: a zero window, no output at all, and
    an error message on the output stream.
    </summary>
    <remarks>
    The last of these is the trap. When the display is gone xprop prints a
    complaint on stdout rather than failing loudly, so a parser that only
    looked for a hex number after the hash would happily read part of the
    error text. All three must collapse to None, because the watcher treats
    None as no focus and holds the previous page rather than acting on
    rubbish. A genuine zero window is normal: it happens every time focus
    passes between windows.
    </remarks>
    """
    assert focus.parse_active_window_id("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n") is None
    assert focus.parse_active_window_id("") is None
    assert focus.parse_active_window_id("xprop: unable to open display\n") is None


def test_the_class_and_title_are_read():
    """<summary>
    Pins which half of WM_CLASS counts as the application, and which title
    property wins.
    </summary>
    <remarks>
    WM_CLASS holds two strings: the instance name first and the class name
    second. Here they are "Navigator" and "firefox", and it is the second
    that people mean when they write a rule for a browser, so taking the
    first would make every sensible rule miss. The title is read from
    _NET_WM_NAME in preference to WM_NAME because only the former is
    reliably UTF-8; the sample deliberately carries both.
    </remarks>
    """
    window = focus.parse_window_properties(PROPERTIES)
    assert window.app == "firefox"
    assert window.title == "Deckplate on GitHub - Mozilla Firefox"


def test_a_single_class_string_is_read():
    """<summary>
    Pins the case where WM_CLASS carries only one string.
    </summary>
    <remarks>
    Plenty of applications set just the one, so code that always reaches for
    the second value would raise or return empty for a large slice of the
    desktop. That is not hypothetical tidiness: a rule targeting a simple
    editor would never fire.
    </remarks>
    """
    window = focus.parse_window_properties('WM_CLASS = "gedit"\nWM_NAME = "notes.txt"\n')
    assert window.app == "gedit" and window.title == "notes.txt"


def test_a_window_with_no_properties_is_empty():
    """<summary>
    Pins a window that answers "not found" as an empty Window rather than a
    None or a raise.
    </summary>
    <remarks>
    This happens when the window disappears between asking for its id and
    asking for its properties, which is common enough with menus and splash
    windows. An empty Window matches no rule, so the deck simply keeps the
    page it has. Returning None instead would be a different value with a
    different meaning downstream, and raising would kill the watcher thread.
    </remarks>
    """
    window = focus.parse_window_properties("WM_CLASS:  not found.\n")
    assert window == Window()


def test_matching_is_a_case_insensitive_search():
    """<summary>
    Pins how a user's focus rule is matched: a case insensitive regular
    expression searched against both the application and the title.
    </summary>
    <remarks>
    Every line here is a decision someone could reasonably reverse, so they
    are all pinned. The match is a search and not a full match, so "github"
    finds it inside a longer title. The title is searched as well as the
    application, which is what lets one browser hold different pages per
    site. It really is an expression, so an anchor works and a user's stray
    bracket is meaningful rather than literal.

    The two negatives at the end are the safety rail. No window at all must
    match nothing, and an empty rule must match nothing rather than
    everything: an empty pattern searched as a regular expression matches
    every string, which would make one blank rule swallow the whole deck and
    pin it to a single page.
    </remarks>
    """
    window = Window(app="firefox", title="Deckplate on GitHub - Mozilla Firefox")
    assert focus.matches("firefox", window)
    assert focus.matches("FIREFOX", window)
    assert focus.matches("github", window)          # the title counts too
    assert focus.matches(r"^fire", window)          # and it is a real expression
    assert not focus.matches("thunderbird", window)
    assert not focus.matches("firefox", None)
    assert not focus.matches("", window)


def test_a_broken_expression_matches_nothing_rather_than_raising():
    """<summary>
    Pins a malformed rule as matching nothing, with no exception.
    </summary>
    <remarks>
    The pattern comes from a hand edited configuration file, so an unclosed
    bracket is a matter of when, not if. Matching is called from the key
    dispatch path on every focus change, so a raise here would take out the
    whole deck over one mistyped rule. Matching nothing leaves the rest of
    the configuration working while the bad rule silently does nothing.
    </remarks>
    """
    assert not focus.matches("(unclosed", Window(app="firefox"))


def test_wayland_is_reported_rather_than_attempted(monkeypatch):
    """<summary>
    Pins Wayland as a named, reported limitation rather than something the
    reader tries and fails at.
    </summary>
    <remarks>
    Wayland deliberately refuses to tell an ordinary client which window has
    focus, so there is nothing to attempt: xprop would either fail or, worse
    under an X compatibility layer, answer about the wrong thing. The reason
    string must name Wayland, because the only way a user can act on this is
    to know that their session type is the cause rather than a broken
    install. The reader returning None is checked in the same breath so the
    two cannot drift apart.
    </remarks>
    """
    monkeypatch.setattr(focus.sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    reason = focus.unsupported_reason()
    assert reason is not None and "Wayland" in reason
    assert focus.read_window() is None


def test_an_x11_session_is_supported(monkeypatch):
    """<summary>
    Pins a normal X11 session as supported, with no reason to refuse.
    </summary>
    <remarks>
    The counterpart to the Wayland test, and the one that catches an over
    eager guard. WAYLAND_DISPLAY is explicitly removed because it can be
    left set in the environment of a session that is otherwise plain X11,
    and treating its mere presence as proof of Wayland would switch focus
    following off for users who have it working.
    </remarks>
    """
    monkeypatch.setattr(focus.sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert focus.unsupported_reason() is None


def test_the_x11_reader_uses_the_two_xprop_calls(monkeypatch):
    """<summary>
    Pins the two step read: ask the root for the focused window id, then ask
    that window for its class and title.
    </summary>
    <remarks>
    The second assertion is the load bearing one. It checks that the id
    parsed from the first call is what gets passed to the second, which is
    the join between the two halves and the place a refactor breaks things
    without either parser being wrong. The fake decides which reply to give
    by looking for the root flag in the arguments, so the call order itself
    is part of what is pinned.
    </remarks>
    """
    calls = []

    def fake(*arguments):
        """
        <summary>
        Stand-in for the xprop call: the active window id for a root query, otherwise the properties.
        </summary>
        <param name="arguments">The command line that would have run.</param>
        <returns>Canned output text.</returns>
        """
        calls.append(arguments)
        return ACTIVE if "-root" in arguments else PROPERTIES

    monkeypatch.setattr(focus, "_xprop", fake)
    window = focus.x11_window()
    assert window == Window(app="firefox", title="Deckplate on GitHub - Mozilla Firefox")
    assert calls[1][1] == "0x3400007"


def test_the_x11_reader_gives_up_quietly_when_xprop_is_missing(monkeypatch):
    """<summary>
    Pins a missing xprop binary as None rather than a crash.
    </summary>
    <remarks>
    xprop is not installed by default on every desktop, and this is called
    several times a second by the watcher. An exception here would be raised
    on every poll, so the choice is between a silent None and a log that
    fills a disk. The user is told once, by the watcher, not by this.
    </remarks>
    """
    monkeypatch.setattr(focus, "_xprop", lambda *a: "")
    assert focus.x11_window() is None


def test_the_watcher_remembers_the_latest_window():
    """<summary>
    Pins the watcher caching the most recent window so readers get an answer
    without waiting for a poll.
    </summary>
    <remarks>
    ``current`` is read from the key dispatch path, which must not block on
    xprop, so the value has to be whatever the last poll saw. It starts as
    None rather than as an empty Window so that "not polled yet" and "no
    focused window" stay distinguishable. Each poll must replace the cached
    value, not merge into it: a stale application name would send deck
    presses to the page for a window the user left minutes ago.
    </remarks>
    """
    windows = [Window(app="firefox"), Window(app="code")]
    watcher = FocusWatcher(0.01, read=lambda: windows.pop(0), log=lambda t: None)
    assert watcher.current is None
    assert watcher.poll_once() == Window(app="firefox")
    assert watcher.current == Window(app="firefox")
    watcher.poll_once()
    assert watcher.current == Window(app="code")


def test_the_watcher_survives_a_reader_that_raises():
    """<summary>
    Pins a throwing reader as survivable, and pins the complaint to once
    rather than once per poll.
    </summary>
    <remarks>
    The count of one after two failing polls is the whole test. The watcher
    runs on a timer several times a second, so a log line per failure would
    produce thousands of identical lines a minute and bury anything useful,
    on a fault that is usually permanent for the session anyway. Red here
    means either the watcher thread dies on the first display hiccup, or the
    log floods.
    </remarks>
    """
    logged = []

    def angry():
        """
        <summary>
        A reader that raises as if there were no display.
        </summary>
        <exception cref="OSError">Always.</exception>
        """
        raise OSError("no display")

    watcher = FocusWatcher(0.01, read=angry, log=logged.append)
    assert watcher.poll_once() is None
    assert watcher.poll_once() is None
    assert len(logged) == 1  # said once, not five times a second


def test_the_watcher_thread_starts_and_stops(monkeypatch):
    """<summary>
    Pins the watcher as restartable: stopped and started again it must poll
    once more.
    </summary>
    <remarks>
    A configuration reload stops and starts the watcher, so a watcher that
    can only run once would leave focus following dead until the daemon was
    restarted, with everything else looking healthy. A thread object cannot
    be started twice, so the second start has to build a fresh one; that is
    exactly the mistake this catches. The event is cleared between the two
    runs so the second wait cannot be satisfied by the first run's work.
    </remarks>
    """
    monkeypatch.setattr(focus, "unsupported_reason", lambda: None)
    seen = threading.Event()

    def read():
        """
        <summary>
        Signal that a read happened and return a fixed Firefox window.
        </summary>
        <returns>A Window.</returns>
        """
        seen.set()
        return Window(app="firefox")

    watcher = FocusWatcher(0.01, read=read, log=lambda t: None)
    watcher.start()
    assert seen.wait(2)
    watcher.stop()
    assert watcher.current == Window(app="firefox")
    # Stopped and started again, as a config reload does, it must run once more.
    seen.clear()
    watcher.start()
    assert seen.wait(2)
    watcher.stop()


def test_the_watcher_does_not_start_where_the_desktop_cannot_answer(monkeypatch):
    """<summary>
    Pins the watcher refusing to start at all when the desktop cannot report
    focus, and saying why in the log.
    </summary>
    <remarks>
    Starting a thread that can only ever fail wastes a poll several times a
    second for the life of the daemon. The reason from
    <see cref="focus.unsupported_reason"/> is passed straight through to the
    log because it is the user's only clue that focus rules in their
    configuration are being ignored for an environmental reason and not
    because they wrote them wrongly. ``current`` staying None proves the
    read was never attempted, since the fake reader would have returned a
    window had it been called.
    </remarks>
    """
    monkeypatch.setattr(focus, "unsupported_reason", lambda: "no display here")
    logged = []
    watcher = FocusWatcher(0.01, read=lambda: Window(app="firefox"), log=logged.append)
    watcher.start()
    assert watcher.current is None
    assert logged and "no display here" in logged[0]
