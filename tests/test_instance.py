"""<summary>
The single instance lock that keeps two daemons off one deck.
</summary>
<remarks>
Every test passes its own lock file under the temporary folder, so the suite
never meets a daemon that is really running on the machine and never stops
one from starting. Nothing here opens a deck.
</remarks>
"""

import pytest

from deckplate import instance


def test_a_second_claim_is_refused_until_the_first_lets_go(tmp_path):
    """<summary>
    Pins the lock: one holder at a time, and free again once the holder has
    closed it.
    </summary>
    <remarks>
    Two daemons on one deck would interleave their image transfers, so the
    second must be refused before it opens anything. The release half matters
    as much: the lock has to go with the holder, with nothing left to clear by
    hand, or a daemon that was killed could never be started again.
    </remarks>
    """
    path = tmp_path / "deckplate.lock"
    first = instance.claim(path)
    assert first is not None
    with pytest.raises(instance.AlreadyRunning):
        instance.claim(path)
    first.close()
    again = instance.claim(path)
    assert again is not None
    again.close()


def test_a_lock_file_that_cannot_be_made_does_not_stop_the_daemon(tmp_path):
    """<summary>
    Pins a lock file that cannot be created as None, not as another daemon.
    </summary>
    <remarks>
    A missing or read only temporary folder is not evidence that a daemon is
    running. The caller logs it and carries on.
    </remarks>
    """
    assert instance.claim(tmp_path / "no-such-folder" / "deckplate.lock") is None
