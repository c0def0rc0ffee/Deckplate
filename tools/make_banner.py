#!/usr/bin/env python3
"""<summary>
Draw the Deckplate banner and write docs/images/banner.png.

    python tools/make_banner.py

The application icon at the left, the name beside it and one line saying what
the program is, on the deck's own dark surround. It is the picture at the top
of the README and nothing else uses it.
</summary>
<remarks>
A documentation tool, not part of the program. It touches no hardware, opens
no device and imports nothing from the device or protocol layers. Its output
is committed: run it when the icon or the line under the name changes, not on
every build.

The icon comes from <see cref="make_icon.draw"/> rather than being drawn
again here, so the banner cannot drift away from the .ico and the SVG the way
a third copy of the geometry would.
</remarks>"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deckplate import images  # noqa: E402
from make_icon import draw as draw_icon  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "images" / "banner.png"

WIDTH = 1200
HEIGHT = 230
SURROUND = (16, 22, 32)
ICON_SIZE = 150
MARGIN = 56
TITLE = "Deckplate"
STRAPLINE = "Pictures, a clock, the weather, hotkeys and macros on a 15 key LCD deck"


def render() -> Image.Image:
    """<summary>
    The finished banner.
    </summary>
    <returns>An opaque picture <see cref="WIDTH"/> by <see cref="HEIGHT"/>.</returns>
    <remarks>
    The two lines of text are placed from the icon's centre rather than from
    the top, so changing the icon size moves the name and the line under it
    with it instead of leaving them behind. The line under the name is
    measured and shrunk until it fits the width, so rewording it cannot push
    it off the edge of the banner unnoticed.
    </remarks>
    """
    banner = Image.new("RGB", (WIDTH, HEIGHT), SURROUND)
    icon = draw_icon(ICON_SIZE)
    middle = HEIGHT // 2
    banner.paste(icon, (MARGIN, middle - ICON_SIZE // 2), icon)

    from PIL import ImageDraw
    canvas = ImageDraw.Draw(banner)
    left = MARGIN + ICON_SIZE + 40
    canvas.text((left, middle - 12), TITLE, fill=images.FOREGROUND,
                font=images.load_font(62), anchor="ls")
    room = WIDTH - MARGIN - left
    size = 24
    while size > 14 and canvas.textlength(STRAPLINE, font=images.load_font(size)) > room:
        size -= 1
    canvas.text((left, middle + 40), STRAPLINE, fill=images.ACCENT,
                font=images.load_font(size), anchor="ls")
    return banner


def main() -> int:
    """<summary>
    Draw the banner and write it, overwriting what is there.
    </summary>
    <returns>0. Nothing here fails quietly enough to need a status of its own.</returns>
    """
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    banner = render()
    banner.save(TARGET)
    print(f"wrote {TARGET.relative_to(ROOT)} ({banner.width} by {banner.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
