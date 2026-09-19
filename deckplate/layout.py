"""<summary>
Physical positions on the deck versus the key numbers the firmware uses.
</summary>
<remarks>
Read off the real deck with numbered tiles showing (7 September 2026), the
grid looks like this from the front, with the display only strip on the right:

    13  10   7   4   1  |  16
    14  11   8   5   2  |  17
    15  12   9   6   3  |  18

So the firmware numbers keys down each column, starting at the right hand LCD
column. The three strip panels take images but have no button under them; the
three physical side buttons next to the strip never report at all.

This is the trap the whole module exists for. Key 1 is the top right key, not
the top left, and the numbers run down a column rather than across a row, so
any code that treats a key number as reading order puts every picture in the
wrong place and the mistake looks like a drawing fault rather than a numbering
one. Everything above this module speaks in rows and columns counted from the
top left, and only this module knows the firmware's numbering. Converting
anywhere else is how the two schemes get mixed up.

Pure arithmetic, no hardware and no state, so all of it is unit tested.
</remarks>
"""

from __future__ import annotations

ROWS = 3
LCD_COLUMNS = 5
STRIP_COLUMN = 5
COLUMNS = LCD_COLUMNS + 1
KEY_COUNT = ROWS * COLUMNS

# The strip panels are numbered after all fifteen LCD keys, straight down the
# column, which is why they can be split off with a single comparison.
FIRST_STRIP_KEY = 16


def key_number(row: int, column: int) -> int:
    """<summary>
    Device key number (from 1) for a position counted from the top left.
    </summary>
    <param name="row">Row from 0 at the top, up to ROWS minus 1.</param>
    <param name="column">Column from 0 at the left. Column 5 is the strip.</param>
    <returns>The firmware's key number, 1 to 18.</returns>
    <remarks>
    The result counts from 1, not from 0, because that is what the wire uses.
    Feeding it back in as a list index is the usual way this goes wrong.
    The column is reversed on purpose: the firmware starts its numbering at
    the right hand column, so column 0 on the left is the highest run of
    numbers rather than the lowest.
    </remarks>
    
    <exception cref="ValueError">The row or column is off the grid.</exception>"""
    if not 0 <= row < ROWS:
        raise ValueError(f"row {row} is outside 0 to {ROWS - 1}")
    if not 0 <= column < COLUMNS:
        raise ValueError(f"column {column} is outside 0 to {COLUMNS - 1}")
    if column == STRIP_COLUMN:
        return FIRST_STRIP_KEY + row
    return (LCD_COLUMNS - 1 - column) * ROWS + row + 1


def position(key: int) -> tuple[int, int]:
    """<summary>
    (row, column) counted from the top left for a device key number.
    </summary>
    <param name="key">Device key number from 1 to 18, as the deck reports it.</param>
    <returns>The row and column, both counted from 0 at the top left.</returns>
    <remarks>
    The exact inverse of <see cref="key_number"/>, and the pair are tested
    round trip against each other so neither can drift. A key press arrives
    as a number and must come through here before anything else looks at it,
    since the number on its own says nothing about where the key sits.
    </remarks>
    
    <exception cref="ValueError">The key number is outside 1 to 18.</exception>"""
    if not 1 <= key <= KEY_COUNT:
        raise ValueError(f"key {key} is outside 1 to {KEY_COUNT}")
    if key >= FIRST_STRIP_KEY:
        return key - FIRST_STRIP_KEY, STRIP_COLUMN
    index = key - 1
    return index % ROWS, LCD_COLUMNS - 1 - index // ROWS


def is_strip(key: int) -> bool:
    """<summary>
    True for the three display only panels that cannot be pressed.
    </summary>
    <param name="key">Device key number from 1.</param>
    <returns>True for keys 16 to 18, False for the fifteen LCD keys.</returns>
    <remarks>
    A strip panel takes a picture like any other key but never sends a press,
    so an action bound to one can be saved and will simply never fire. Worth
    checking before offering an action in the interface, rather than letting
    someone configure something that cannot work. Out of range numbers answer
    False rather than raising, because callers use this as a filter.
    </remarks>
    """
    return FIRST_STRIP_KEY <= key <= KEY_COUNT


def grid() -> list[list[int]]:
    """<summary>
    The key numbers laid out as rows, exactly as seen from the front.
    </summary>
    <returns>Three rows of six numbers, the strip last in each row.</returns>
    <remarks>
    Built from <see cref="key_number"/> rather than written out as a table, so
    it cannot disagree with the conversion the rest of the code uses. Meant
    for printing and for the configuration page, not for a drawing loop.
    </remarks>
    """
    return [[key_number(row, column) for column in range(COLUMNS)] for row in range(ROWS)]
