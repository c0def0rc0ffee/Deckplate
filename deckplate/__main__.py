"""<summary>
Entry point for ``python -m deckplate`` and for the single file build.
</summary>
<remarks>
Deliberately almost empty. Everything it does is hand the process over to
<see cref="cli.main"/>, so that running the package, running the installed
console script and running the frozen executable all take the same path and
cannot drift apart.

The import of cli is absolute on purpose: PyInstaller runs this file as a
plain script, where a relative import has no parent package to resolve
against and fails at start up rather than in a test.

The exit status is whatever cli.main returns, passed straight to sys.exit,
so a shell or a desktop entry sees the daemon's own result rather than a
plain success.
</remarks>
"""

import sys

from deckplate.cli import main

if __name__ == "__main__":
    sys.exit(main())
