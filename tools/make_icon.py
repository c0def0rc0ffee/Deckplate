#!/usr/bin/env python3
"""<summary>
Draw the Deckplate icon and write deploy/deckplate.ico.

    python tools/make_icon.py

Writes 256, 128, 64, 48, 32 and 16 pixel images into the one .ico. Safe to
run anywhere; touches nothing but that file.
</summary>
<remarks>
The icon is the same picture as deploy/deckplate.svg, a dark plate with three
rows of five keys and the display strip down the right, drawn here with Pillow
because a Windows shortcut and the exe need an .ico and the SVG cannot be
rasterised without tools this machine may not have. Pure rectangles, so the two
stay identical by construction: the geometry below is the SVG's at 64 units,
scaled to each size.

That also means the two are only identical while someone keeps them so. Change
the SVG and this file has to be changed to match by hand, then rerun, or the
Windows icon and the Linux one quietly drift apart.

This is a generator, not part of the program. Nothing imports it, the built
executable does not run it, and its output is committed: run it when the
picture changes, not on every build.

It touches no hardware and reads no device. It writes one file.
</remarks>"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "deploy" / "deckplate.ico"
SIZES = [256, 128, 64, 48, 32, 16]

PLATE = "#1b242e"
KEY = "#2c3a48"
SIGNAL = "#5c9bde"
STRIP = "#0f1620"
LIT = {(0, 0), (1, 0), (0, 1), (4, 2)}  # column, row of the keys drawn in blue


def draw(size: int) -> Image.Image:
    """<summary>
    Render one square image of the icon at ``size`` pixels a side.
    </summary>
    <param name="size">The finished edge length in pixels, one of
    <see cref="SIZES"/>.</param>
    <returns>An RGBA image with a transparent background outside the
    plate.</returns>
    <remarks>
    Drawn four times larger and shrunk with a good filter, because Pillow draws
    hard edged rectangles: at 16 pixels the keys would otherwise be a row of
    aliased blocks. That is the whole reason for the scale factor, so do not
    remove it to make the code shorter.

    Every coordinate is in the SVG's 64 unit grid and converted on the way out,
    so the numbers here can be compared with the SVG line by line.
    </remarks>
    """
    scale = 4  # draw big and shrink, so the small sizes get antialiased edges
    big = size * scale
    unit = big / 64
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)

    def box(x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
        """
        <summary>
        A rectangle in unit coordinates scaled to pixels, in the order PIL wants.
        </summary>
        <param name="x">Left.</param>
        <param name="y">Top.</param>
        <param name="w">Width.</param>
        <param name="h">Height.</param>
        <returns>(x0, y0, x1, y1) in pixels.</returns>
        """
        return (x * unit, y * unit, (x + w) * unit, (y + h) * unit)

    canvas.rounded_rectangle(box(2, 14, 60, 36), radius=6 * unit, fill=PLATE,
                             outline=SIGNAL, width=max(1, round(2 * unit)))
    for row in range(3):
        for column in range(5):
            colour = SIGNAL if (column, row) in LIT else KEY
            canvas.rounded_rectangle(box(8 + column * 8, 21 + row * 8, 6, 6), radius=unit, fill=colour)
        canvas.rounded_rectangle(box(49, 21 + row * 8, 6, 6), radius=unit, fill=STRIP,
                                 outline=SIGNAL, width=max(1, round(unit)))
    return image.resize((size, size), Image.LANCZOS)


def main() -> int:
    """<summary>
    Render every size and save them all into the one multi resolution .ico.
    </summary>
    <returns>0. There is nothing here that can fail quietly enough to need a
    status of its own.</returns>
    <remarks>
    Takes no arguments and always writes to <see cref="TARGET"/>, overwriting
    it. The largest size is saved as the base image and the rest are appended,
    which is the order Pillow expects: hand it the small one first and Windows
    shows a blurred icon at large sizes.
    </remarks>
    """
    images =[draw(size) for size in SIZES]
    images[0].save(TARGET, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[1:])
    print(f"wrote {TARGET.relative_to(ROOT)} with sizes {SIZES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
