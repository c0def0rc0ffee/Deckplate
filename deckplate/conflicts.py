"""<summary>
Find and stop other software that would fight over the deck.
</summary>
<remarks>
The official SOOMFON app ("Stream Controller.exe", Mirabox's StreamDock in a
different badge) holds the same HID interface and swallows every key report,
so running both at once means neither works properly. On Linux the same is
true of OpenDeck or StreamController if they have the deck open.

Nothing here decides anything. It lists what is running, stops what it is
told to stop, and leaves the asking to the command line. That split is the
whole design: this module is the only place that can stop somebody else's
program, so the decision to do it is kept out of it and made where a person
can be asked first.

The symptom this exists for is a deck that enumerates, opens without error
and then never reports a press, because the other program already holds the
interface. It reads as a broken deck rather than as two programs sharing one,
which is why the daemon checks at startup instead of waiting to be asked.

Matching is by process name as a substring, so it is a guess by nature. The
list is kept deliberately narrow: stopping something unrelated is far worse
than missing a conflict, which merely leaves the user to close it by hand.
</remarks>
"""

from __future__ import annotations

import csv
import io
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

# Process names, lower case, compared as substrings. Keep this list narrow:
# a false match would stop something unrelated.
CONFLICTING_NAMES = (
    "stream controller",   # the official SOOMFON app on Windows
    "streamdock",          # Mirabox's own branding of the same app
    "soomfon",
    "opendeck",
    "streamcontroller",    # Core447's Linux app
)


@dataclass(frozen=True)
class Process:
    """<summary>
    One running program, as far as this module cares about it.
    </summary>
    <remarks>
    A snapshot rather than a handle: the program may already have exited by
    the time anything is done with it, which is why <see cref="stop"/> treats
    a process that has gone as success rather than as a fault. The name is
    whatever the platform reported, in its own spelling and case, so compare
    it through <see cref="is_conflicting"/> and not directly.
    </remarks>"""

    pid: int
    name: str


def is_conflicting(name: str) -> bool:
    """<summary>
    Whether a process name looks like software that would hold the deck.
    </summary>
    <param name="name">A process name in any case, as the platform reported it.</param>
    <returns>True when any entry of CONFLICTING_NAMES appears in it.</returns>
    <remarks>
    A substring match, so a longer name such as "Stream Controller.exe" is
    caught without listing every spelling. That looseness cuts both ways: a
    new entry in the list is a new chance to match something innocent, so
    widen it only for a name that has actually been seen holding the deck.
    </remarks>
    """
    lowered = name.lower()
    return any(marker in lowered for marker in CONFLICTING_NAMES)


def parse_tasklist(text: str) -> list[Process]:
    """<summary>
    Output of `tasklist /FO CSV /NH` on Windows.
    </summary>
    <param name="text">The command's whole standard output.</param>
    <returns>One entry per row that parsed, others skipped.</returns>
    <remarks>
    Split out from the command that produces it so it can be tested on any
    platform against recorded output, which is the only way this path gets
    covered when the suite runs on Linux. The columns are name then pid, the
    opposite way round from ps, which is the easy thing to get backwards.
    A row whose second column is not a number is skipped rather than raising,
    because localised builds print a header or a message the format does not
    describe.
    </remarks>
    """
    found = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2 or not row[1].strip().isdigit():
            continue
        found.append(Process(pid=int(row[1]), name=row[0]))
    return found


def parse_ps(text: str) -> list[Process]:
    """<summary>
    Output of `ps -eo pid=,comm=` on Linux.
    </summary>
    <param name="text">The command's whole standard output.</param>
    <returns>One entry per line that parsed, others skipped.</returns>
    <remarks>
    Columns are pid then name here, the opposite way round from tasklist. The
    name is split off at the first run of whitespace only, so a command name
    with a space in it survives intact. ``comm`` is the short name and is
    truncated by the kernel, which is another reason the matching is by
    substring rather than by equality.
    </remarks>
    """
    found = []
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            found.append(Process(pid=int(parts[0]), name=parts[1]))
    return found


def running_processes() -> list[Process]:
    """<summary>
    Everything running on this machine, however the platform reports it.
    </summary>
    <returns>One entry per process, or an empty list if it could not be asked.</returns>
    <remarks>
    An empty list means either nothing is running, which cannot happen, or the
    listing failed, so treat it as "no conflicts known" and not as proof there
    are none. Failure is swallowed on purpose: a machine with no tasklist or
    no ps must still be able to run the daemon, and the worst outcome of not
    knowing is that the user is left to close the other program themselves.
    The ten second timeout is there because a hung listing would otherwise
    hang startup.
    </remarks>
    """
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=10, check=False).stdout
            return parse_tasklist(out)
        out = subprocess.run(["ps", "-eo", "pid=,comm="], capture_output=True,
                             text=True, timeout=10, check=False).stdout
        return parse_ps(out)
    except (OSError, subprocess.SubprocessError):
        return []


def find_conflicts() -> list[Process]:
    """<summary>
    Everything running that would hold the deck against us.
    </summary>
    <returns>The matching processes, empty when there is nothing to worry about.</returns>
    <remarks>
    This process is excluded by pid, which matters more than it looks: a
    second copy of the daemon would be a genuine conflict and must still be
    listed, while the copy asking the question must never be. Nothing is
    stopped here, and an empty list is not a promise the deck is free, only
    that nothing known was found.
    </remarks>
    """
    own = os.getpid()
    return [p for p in running_processes() if is_conflicting(p.name) and p.pid != own]


def stop(process: Process, wait_seconds: float = 5.0) -> bool:
    """<summary>
    Stop one process and wait for it to go, so the USB handle is released.
    </summary>
    <param name="process">One entry from <see cref="find_conflicts"/>.</param>
    <param name="wait_seconds">How long to wait for it to actually exit.</param>
    <returns>True once it has gone, False on a refusal or a timeout.</returns>
    <remarks>
    Waiting is the point of this, not a courtesy. The signal returns at once
    but the USB handle is not released until the program has finished exiting,
    so opening the deck immediately after asking would fail just as it did
    before. False means do not try to open the deck yet.

    Linux gets SIGTERM and is left to shut down properly; Windows gets
    taskkill with the tree and force flags, because the official app spawns
    helpers that keep the handle after the main window has gone. Never called
    on its own: the command line asks first, which is what the
    ``official_software`` setting decides.
    </remarks>
    """
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=10, check=False)
        else:
            os.kill(process.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        return False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if all(p.pid != process.pid for p in running_processes()):
            return True
        time.sleep(0.25)
    return False


def describe(processes: list[Process]) -> str:
    """<summary>
    The processes as one line of prose, for asking the user about them.
    </summary>
    <param name="processes">Whatever <see cref="find_conflicts"/> returned.</param>
    <returns>Names with pids, comma separated. Empty for an empty list.</returns>
    <remarks>
    The pid is included because two copies of the same program are otherwise
    indistinguishable in the question, and the user is being asked to agree to
    one of them being stopped.
    </remarks>
    """
    return ", ".join(f"{p.name} (pid {p.pid})" for p in processes)
