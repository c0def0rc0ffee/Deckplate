"""<summary>
Spotting other software that holds the same deck, and deciding what to do
about it, with no process ever really listed or stopped.
</summary>
<remarks>
Two separate things are pinned here. The parsers turn captured ``tasklist``
and ``ps`` output into processes, and the narrow name match decides which of
those are rivals for the deck. The policy tests then cover
<see cref="cli.resolve_conflicts"/> with both of those faked out, so the
decision is tested without any real process being signalled.

The sample text at the top is genuine output shape, kept verbatim, because
the point of a parser test is the exact spacing and quoting the real tools
produce. Rewriting it tidily would remove the only thing it proves.
</remarks>
"""

from deckplate import cli, conflicts
from deckplate.conflicts import Process

TASKLIST = '''"System Idle Process","0","Services","0","8 K"
"Stream Controller.exe","30220","Console","1","150,000 K"
"notepad.exe","4242","Console","1","20,000 K"
"RzChromaStreamServer.exe","19684","Console","1","9,000 K"
'''

PS = """    1 systemd
 2231 opendeck
 2300 bash
 2410 streamcontroller
"""


def test_is_conflicting_is_narrow():
    """<summary>
    Pins the rival name match tight enough to catch the badges of the
    official app and loose enough to leave everything else alone.
    </summary>
    <remarks>
    This is a substring match over lower case names, so it is one careless
    marker away from stopping something unrelated. The negatives carry the
    weight: a streaming keyboard lighting service and a plain text editor
    must survive, and so must python itself, or the daemon could be asked to
    kill its own interpreter. Going red on a positive means a rival will sit
    on the HID interface and swallow every key report, so the deck looks
    dead with nothing in the log. Going red on a negative means Deckplate
    kills a stranger's process, which is far worse.
    </remarks>
    """
    assert conflicts.is_conflicting("Stream Controller.exe")
    assert conflicts.is_conflicting("StreamDock.exe")
    assert conflicts.is_conflicting("opendeck")
    assert not conflicts.is_conflicting("RzChromaStreamServer.exe")
    assert not conflicts.is_conflicting("notepad.exe")
    assert not conflicts.is_conflicting("python.exe")


def test_parse_tasklist():
    """<summary>
    Pins the Windows process listing parser against real CSV output.
    </summary>
    <remarks>
    The trap this guards is the memory column: "150,000 K" carries a comma
    inside a quoted field, so a naive split on commas shifts every column
    and the pid becomes rubbish. Reading it through a CSV reader is what
    keeps that right. The header free form is assumed because the daemon
    always asks for it with /NH, and rows whose second column is not digits
    are skipped rather than raising, since the listing can carry oddities
    that are none of our business. Red here means Windows users get either
    no rival detected or a stop aimed at the wrong pid.
    </remarks>
    """
    found = conflicts.parse_tasklist(TASKLIST)
    assert Process(30220, "Stream Controller.exe") in found
    assert Process(4242, "notepad.exe") in found
    assert [p for p in found if conflicts.is_conflicting(p.name)] == [Process(30220, "Stream Controller.exe")]


def test_parse_ps():
    """<summary>
    Pins the Linux process listing parser, including the order it preserves.
    </summary>
    <remarks>
    ``ps -eo pid=,comm=`` right aligns the pid, so every line begins with
    leading spaces of a width that changes with the pid. The parser has to
    split on runs of whitespace rather than a fixed column. Two rivals are
    present on purpose: both must come back, in listing order, because the
    caller stops all of them and names them in one message. Red here on
    Linux means a rival keeps the deck open and the user sees a device busy
    failure with no explanation of who has it.
    </remarks>
    """
    found = conflicts.parse_ps(PS)
    assert [p for p in found if conflicts.is_conflicting(p.name)] == [
        Process(2231, "opendeck"), Process(2410, "streamcontroller")]


def _patch(monkeypatch, found):
    """<summary>
    Replace process discovery and stopping with fakes, and hand back the
    list that records what would have been stopped.
    </summary>
    <param name="monkeypatch">The pytest fixture doing the patching.</param>
    <param name="found">Processes the fake discovery should report.</param>
    <returns>A list that grows one entry per process the policy stopped.</returns>
    <remarks>
    Nothing below this line may reach a real process table or send a real
    signal, so every policy test must go through here. The fake stop returns
    True, meaning it always succeeds, so these tests say nothing about the
    failure path where a rival refuses to die.
    </remarks>
    """
    stopped = []
    monkeypatch.setattr(conflicts, "find_conflicts", lambda: list(found))
    monkeypatch.setattr(conflicts, "stop", lambda p: stopped.append(p) or True)
    return stopped


def test_resolve_nothing_running(monkeypatch):
    """<summary>
    Pins silence as the behaviour when no rival is running at all.
    </summary>
    <remarks>
    The common case is the one a user meets every single start, so it must
    produce no question and no log line whatever. If this went red the
    daemon would either print noise on every launch or, worse, stop at a
    prompt under systemd where nobody can answer it.
    </remarks>
    """
    stopped = _patch(monkeypatch, [])
    log = []
    cli.resolve_conflicts("ask", interactive=True, ask=lambda q: "y", log=log.append)
    assert stopped == [] and log == []


def test_resolve_ask_yes_stops(monkeypatch):
    """<summary>
    Pins the ask policy with a yes: the rival is named in the question and
    is then stopped and reported.
    </summary>
    <remarks>
    The fake answer here is an empty string, not the letter y, which is the
    point: the prompt offers [Y/n] so pressing return alone must mean yes.
    The question is also checked for the rival's name, because a prompt that
    does not say what it is about to kill is not consent. Red here means
    either a bare return no longer agrees, or the user is asked to authorise
    something unnamed.
    </remarks>
    """
    official = Process(30220, "Stream Controller.exe")
    stopped = _patch(monkeypatch, [official])
    asked = []
    log = []
    cli.resolve_conflicts("ask", interactive=True, ask=lambda q: asked.append(q) or "", log=log.append)
    assert stopped == [official]
    assert "Stream Controller.exe" in asked[0]
    assert any("stopped" in line for line in log)


def test_resolve_ask_no_keeps(monkeypatch):
    """<summary>
    Pins a declined prompt as truly declined: nothing is stopped and the
    log says so.
    </summary>
    <remarks>
    This is the half of consent that matters. A no must leave the rival
    running even though the deck will then not work properly, because the
    user may well be mid session in the official app on purpose. Red here
    means Deckplate kills software against an explicit refusal.
    </remarks>
    """
    official = Process(30220, "Stream Controller.exe")
    stopped = _patch(monkeypatch, [official])
    log = []
    cli.resolve_conflicts("ask", interactive=True, ask=lambda q: "n", log=log.append)
    assert stopped == [] and any("leaving" in line for line in log)


def test_resolve_ask_without_terminal_warns_and_keeps(monkeypatch):
    """<summary>
    Pins the headless case: when there is no terminal to ask at, the ask
    policy warns and stops nothing.
    </summary>
    <remarks>
    This is the systemd and autostart path, where ``ask`` is still the
    configured default but no human is present. Note the fake would answer
    yes if it were called, so the assertion that nothing was stopped proves
    the prompt was skipped rather than answered. Failing closed is
    deliberate: killing another user's app with nobody watching is the wrong
    default, and the warning names the two ways to choose otherwise. Red
    here means an unattended daemon silently kills processes.
    </remarks>
    """
    stopped = _patch(monkeypatch, [Process(1, "opendeck")])
    log = []
    cli.resolve_conflicts("ask", interactive=False, ask=lambda q: "y", log=log.append)
    assert stopped == [] and any("warning" in line for line in log)


def test_resolve_stop_and_keep_policies(monkeypatch):
    """<summary>
    Pins the two policies that never ask: ``stop`` acts with no terminal,
    ``keep`` only notes the rival even when a terminal is there.
    </summary>
    <remarks>
    The combinations are crossed on purpose. ``stop`` is given
    ``interactive=False`` and an answering fake that would say no, proving
    it neither needs nor consults the prompt, which is what makes it usable
    under systemd. ``keep`` is given a terminal and a fake that would say
    yes, proving it never asks and never stops. Red here means a configured
    policy is being overridden by whether a terminal happens to be
    attached.
    </remarks>
    """
    official = Process(30220, "Stream Controller.exe")
    stopped = _patch(monkeypatch, [official])
    log = []
    cli.resolve_conflicts("stop", interactive=False, ask=lambda q: "n", log=log.append)
    assert stopped == [official]
    stopped.clear()
    cli.resolve_conflicts("keep", interactive=True, ask=lambda q: "y", log=log.append)
    assert stopped == [] and any("note" in line for line in log)
