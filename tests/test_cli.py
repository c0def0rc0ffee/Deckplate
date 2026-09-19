"""<summary>
Argument parsing in the command line that has no deck behind it.
</summary>
<remarks>
Only the pure parsing helpers belong here. The commands themselves open the
deck, so they are exercised through the controller tests with a fake
transport instead. Nothing in this file may grow a call that reaches
hardware.
</remarks>
"""

import pytest

from deckplate import cli


def test_parse_pitch_reads_two_numbers_across_then_down():
    """<summary>
    Pins the spellings of a key pitch the command line must accept, and the
    range it must refuse.
    </summary>
    <remarks>
    The pitch is how far apart the key centres sit on the one physical
    screen, so a wrong pair does not raise anywhere later: it silently cuts
    every tile from the wrong place and the whole deck shows smeared
    pictures. Comma and the letter x are both accepted because the
    calibration output prints one and people type the other, and surrounding
    spaces are tolerated because they come from copying a printed value.
    If this went red the likely causes are the separator set narrowing, or
    the bounds check in <see cref="config.KEY_PITCH_RANGE"/> no longer being
    applied, which would let a nonsense pitch reach the tile cutter.
    </remarks>
    """
    assert cli.parse_pitch("142,160") == (142, 160)
    assert cli.parse_pitch("142 x 160") == (142, 160)
    assert cli.parse_pitch(" 95, 95 ") == (95, 95)
    for bad in ("142", "142,160,1", "a,b", "10,160", "142,999", ""):
        with pytest.raises(ValueError):
            cli.parse_pitch(bad)
