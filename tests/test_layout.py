"""<summary>
The grid as read off the real deck with numbered tiles showing.
</summary>
<remarks>
The key numbering is not a design decision, it is an observation: numbered
tiles were pushed to the hardware and the arrangement written down. Everything
here follows from that one table, so a change to the grid means either the
observation was wrong or a different deck is being driven.

A fault in this mapping is silent and confusing in use. Nothing errors, but
pressing a key runs the action configured for a different key, and pictures
appear in the wrong places.
</remarks>
"""

import pytest

from deckplate import layout

SEEN_ON_DECK = [
    [13, 10, 7, 4, 1, 16],
    [14, 11, 8, 5, 2, 17],
    [15, 12, 9, 6, 3, 18],
]


def test_grid_matches_what_rob_saw():
    """<summary>
    The grid is the arrangement observed on the hardware, column by column.
    </summary>
    <remarks>
    Key numbers run down each column rather than across each row, which is the
    opposite of what the shape of the deck suggests. That is why this is pinned to
    a written down observation: the natural guess is wrong.
    </remarks>
    """
    assert layout.grid() == SEEN_ON_DECK


def test_key_number_and_position_round_trip():
    """<summary>
    Converting a position to a key number and back gives the original position for
    every key on the deck.
    </summary>
    <remarks>
    The two directions are used on opposite sides of the daemon: incoming presses
    arrive as key numbers, while the configuration file is written in rows and
    columns. If they ever disagreed, a press would be attributed to one tile while
    the picture was drawn on another.
    </remarks>
    """
    for row, keys in enumerate(SEEN_ON_DECK):
        for column, key in enumerate(keys):
            assert layout.key_number(row, column) == key
            assert layout.position(key) == (row, column)


def test_every_key_number_used_exactly_once():
    """<summary>
    The grid covers 1 to 18 with no number missing and none repeated.
    </summary>
    <remarks>
    A duplicate would make two positions fight over one screen region, and a gap
    would leave a key that can be pressed but never drawn. Neither shows up as an
    error at runtime, so it is checked as a property of the table itself.
    </remarks>
    """
    keys = sorted(k for row in layout.grid() for k in row)
    assert keys == list(range(1, 19))


def test_strip_detection():
    """<summary>
    Keys 16 to 18 are the side strip and the rest are ordinary keys.
    </summary>
    <remarks>
    The strip carries the clock, date and weather tiles and takes no user action,
    so this is what decides whether a press is routed to a configured action or
    ignored. Widening it would silence three real keys.
    </remarks>
    """
    assert not layout.is_strip(1)
    assert not layout.is_strip(15)
    assert layout.is_strip(16)
    assert layout.is_strip(18)


def test_bounds():
    """<summary>
    A row, column or key number outside the deck raises rather than being clamped.
    </summary>
    <remarks>
    Clamping would turn a bad configuration file into a press on a real key that
    was never asked for. Raising sends it back to the caller, where the
    configuration loader can report the line at fault.
    </remarks>
    """
    with pytest.raises(ValueError):
        layout.key_number(3, 0)
    with pytest.raises(ValueError):
        layout.key_number(0, 6)
    with pytest.raises(ValueError):
        layout.position(0)
    with pytest.raises(ValueError):
        layout.position(19)
