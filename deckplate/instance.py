"""<summary>
The single instance lock: one daemon per user, however it was started.
</summary>
<remarks>
Two daemons on one deck is not a harmless duplicate. Both hidraw on Linux and
the Windows HID layer let a device be opened twice, so nothing underneath
refuses the second one, and the two would interleave their image transfers on
the wire: a BAT header from one, chunks from the other. Every packet would be
a proven command and the sequence would be one nothing has ever proven.

The local port cannot be the guard. The server can be switched off or moved
in the config, and the daemon deliberately carries on without it when the
port is taken. So the guard is a lock on a file, held for the life of the
process. The operating system drops the lock when the process ends, however
it ends, so there is no stale lock to clear after a crash or a kill, and the
file itself is left where it is.

Only ``run`` takes the lock. The one shot commands (show, clear, probe and
the rest) do not, and still must not be used while the daemon is running, as
their own help says.
</remarks>
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import IO

LOCK_NAME = "deckplate.lock"


class AlreadyRunning(RuntimeError):
    """<summary>
    Another daemon holds the lock, so this one must not open the deck.
    </summary>"""


def lock_path() -> Path:
    """<summary>
    Where the lock file lives for the current user.
    </summary>
    <returns>A path in a per user folder, which may not exist yet.</returns>
    <remarks>
    On Linux this is the session's runtime folder when there is one, which is
    private to the user and cleared at logout. Without one it falls back to
    the temporary folder with the user id in the name, because that folder is
    shared between users and each needs a lock of their own. On Windows the
    temporary folder is already per user.
    </remarks>
    """
    if sys.platform == "win32":
        return Path(tempfile.gettempdir()) / LOCK_NAME
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime) / LOCK_NAME
    return Path(tempfile.gettempdir()) / f"deckplate-{os.getuid()}.lock"


def claim(path: Path | None = None) -> IO[str] | None:
    """<summary>
    Take the single instance lock, or find out that another daemon has it.
    </summary>
    <param name="path">The lock file, or None for <see cref="lock_path"/>. The
    tests pass their own so they never meet a daemon that is really running.</param>
    <returns>The open lock file. The caller must keep hold of it for as long
    as the daemon runs: the lock is released when it is closed or collected.
    None when the file could not be created at all, which is reported by the
    caller and does not stop the daemon.</returns>
    <remarks>
    The lock is advisory and never blocks: it is asked for once and either
    granted or refused. A file that cannot be created (a read only or missing
    temporary folder) is not treated as another daemon, because refusing to
    start over that would trade a rare double start for a daemon that will
    not run at all.
    </remarks>

    <exception cref="AlreadyRunning">Another process holds the lock.</exception>"""
    target = path if path is not None else lock_path()
    try:
        handle = open(target, "a+", encoding="utf-8")
    except OSError:
        return None
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        handle.close()
        raise AlreadyRunning("deckplate is already running for this user; stop it before starting another") from err
    return handle
