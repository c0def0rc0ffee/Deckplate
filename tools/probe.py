#!/usr/bin/env python3
"""<summary>
Hardware probe for the Soomfon Stream Controller. A thin wrapper that runs
``deckplate probe`` from the source tree, so the old milestone 1 command keeps
working now that the protocol code lives in the deckplate package.

    python tools/probe.py --seconds 60
</summary>
<remarks>
This is the one file in tools that reaches real hardware, so read this before
touching it. It sends nothing of its own. Every byte that leaves this process
is built by <see cref="deckplate.protocol"/> and written by
<see cref="deckplate.device.Deck"/>, which between them allow only the seven
proven commands: DIS, CONNECT, LIG, CLE, HAN, BAT and STP. Both of those gates
open only for an explicit opt in, ``experimental=True`` on the protocol side
and ``allow_experimental=True`` on the device side, and this wrapper passes
neither. Leave it that way.

Nothing here may be widened on a guess. Not one new command, not one new
argument value, not a size or a mode "that ought to work". A command is added
only as a byte for byte copy of a USB capture of the official app, reviewed
before it ships. A deck was permanently disabled on 10 September 2026 by
guessed values sent from a scratch script: it no longer enumerates and there
is no known recovery. Learning is done by listening to a capture, never by
sending. A cosmetic problem is never worth that risk.

The probe is also a listener in the ordinary sense: it opens the deck, prints
the key events it reads and exits. Anything it prints is observation, not
permission to send more. Route new work through the daemon, which sets neither
flag, rather than through a script of your own against a real deck.

<see cref="deckplate.cli.main"/> owns the argument parsing; the extra arguments
on the command line are passed through to the ``probe`` subcommand untouched.

Findings from the first hardware session (7 September 2026, Windows):
  - Numbered tiles appeared upright with rotate 90 and mirror both.
  - Keys are numbered down each column, columns counted from the RIGHT:
        13 10  7  4  1 | 16
        14 11  8  5  2 | 17
        15 12  9  6  3 | 18
    The right column is the display strip: screens with no buttons. The three
    physical side buttons report nothing anywhere, not even as keystrokes.
  - Input reports start with the text ACK, byte 9 is the key number from 1,
    byte 10 is 1 for press and 0 for release.
  - The official SOOMFON app must be closed first or it swallows every report.
</remarks>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deckplate.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["probe", *sys.argv[1:]]))
