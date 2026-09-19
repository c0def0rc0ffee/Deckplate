"""<summary>
The desktop module: finding a window by pattern and the xdotool lines that
act on it.
</summary>
<remarks>
No window is touched. The xdotool desktop is built over a recorder, and the
assertions are on the command lines, because a wrong verb there is a key
that quietly does nothing or, worse, acts on the wrong window.
</remarks>
"""

import subprocess

import pytest

from deckplate import desktop


class Runner:
    """<summary>Records every command and answers from a queue of outputs.</summary>"""

    def __init__(self, *answers):
        """<summary>Queue the standard output to give back, in order.</summary>"""
        self.calls = []
        self.answers = list(answers)

    def __call__(self, args, **kwargs):
        """<summary>Record the call and answer with the next queued output.</summary>"""
        self.calls.append(args)
        text = self.answers.pop(0) if self.answers else ""
        return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")


def test_find_tries_class_then_classname_then_title():
    """<summary>
    The search runs per field in a fixed order and stops at the first field
    that finds anything, taking the first window listed.
    </summary>
    <remarks>
    Class before title is deliberate: a pattern like "firefox" matches the
    browser's class, and stopping there keeps a terminal whose title happens
    to mention firefox from being picked instead.
    </remarks>
    """
    run = Runner("", "", "41943044\n41943050\n")
    found = desktop.XdotoolDesktop(run).find("firefox")
    assert found == "41943044"
    assert run.calls == [
        ["xdotool", "search", "--onlyvisible", "--class", "firefox"],
        ["xdotool", "search", "--onlyvisible", "--classname", "firefox"],
        ["xdotool", "search", "--onlyvisible", "--name", "firefox"],
    ]
    assert desktop.XdotoolDesktop(Runner("", "", "")).find("nothing") is None


def test_act_runs_the_operation_or_reports_nothing_found():
    """<summary>
    Each operation becomes its xdotool or wmctrl line on the window found,
    and a pattern matching nothing answers False without raising.
    </summary>
    <remarks>
    False rather than an exception is what lets the runner launch the
    program instead, which is the "switch to it or start it" behaviour a
    window key is for. Maximise needs wmctrl and says so when it is missing.
    </remarks>
    """
    run = Runner("7\n", "", "7\n", "", "7\n", "", "", "7\n", "")
    which = lambda name: "/usr/bin/wmctrl"
    d = desktop.XdotoolDesktop(run, which)
    assert desktop.act("code", "focus", d)
    assert desktop.act("code", "minimise", d)
    assert desktop.act("code", "maximise", d)
    assert desktop.act("code", "close", d)
    acted = [call for call in run.calls if call[1] != "search"]
    assert acted == [
        ["xdotool", "windowactivate", "--sync", "7"],
        ["xdotool", "windowminimize", "7"],
        ["wmctrl", "-i", "-r", "7", "-b", "add,maximized_vert,maximized_horz"],
        ["xdotool", "windowactivate", "--sync", "7"],
        ["xdotool", "windowclose", "7"],
    ]
    assert not desktop.act("nothing", "focus", desktop.XdotoolDesktop(Runner("", "", "")))
    with pytest.raises(desktop.DesktopError, match="wmctrl"):
        desktop.act("code", "maximise", desktop.XdotoolDesktop(Runner("7\n"), lambda name: None))
    with pytest.raises(ValueError):
        desktop.act("code", "explode", d)
