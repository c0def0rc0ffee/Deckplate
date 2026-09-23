#!/usr/bin/env python3
"""<summary>
Draw every shipped theme icon as one contact sheet, for the documentation.

    python tools/render_themes.py -o docs/images/themes.png
    python tools/render_themes.py --theme controls --columns 8 -o /tmp/controls.png

Each theme becomes a heading and a grid of keys, every key drawn on the
deck's own surround with the icon's name under it, so the README can show
what comes in the box rather than list it.
</summary>
<remarks>
A documentation tool, not part of the program. It touches no hardware, opens
no device and imports nothing from the device or protocol layers: it asks the
themes module for the same files the daemon draws and lays them out on a
canvas. A deck does not need to be plugged in, and nothing it produces can
reach one.

The picture is deterministic. The icons come off disk in manifest order and
nothing here reads the clock or the network, so regenerating the sheet after
adding an icon gives a diff of that icon rather than of the whole file.

An animated icon is drawn as one frame from the middle of its loop, since a
PNG cannot move, and is marked with a dot in the corner so the sheet does not
claim the deck shows it standing still.
</remarks>"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deckplate import images, themes  # noqa: E402

# Everything is a share of one key, so --size scales the whole sheet.
SURROUND = (16, 22, 32)
TILE = (26, 34, 46)
GAP = 0.14
MARGIN = 0.4
CORNER = 0.12
LABEL_HEIGHT = 0.34
HEADING_HEIGHT = 0.7
ICON_INSET = 0.12
ANIMATED_DOT = 0.09


def draw_icon(path: Path, size: int, animated: bool) -> Image.Image:
    """<summary>
    One icon as a key sized tile with the deck's key background behind it.
    </summary>
    <param name="path">The icon's PNG or GIF.</param>
    <param name="size">The tile's edge length in pixels.</param>
    <param name="animated">True for a GIF, which gets the corner dot.</param>
    <returns>An opaque tile.</returns>
    <remarks>
    The icon is pasted with its own alpha rather than converted, so the flat
    colours keep their edges against the tile the way they do on a key.

    An animated icon is drawn from the middle of its loop rather than the
    first frame. The first frame of a counter is an empty tally and of a ring
    a full circle, so a sheet made from first frames shows several icons that
    look blank or identical.
    </remarks>
    """
    tile = Image.new("RGB", (size, size), TILE)
    inset = round(size * ICON_INSET)
    art = Image.open(path)
    art.seek(getattr(art, "n_frames", 1) // 2)
    art = art.convert("RGBA").resize((size - inset * 2, size - inset * 2), Image.LANCZOS)
    tile.paste(art, (inset, inset), art)
    if animated:
        radius = round(size * ANIMATED_DOT)
        edge = round(size * 0.1)
        ImageDraw.Draw(tile).ellipse(
            [size - edge - radius * 2, edge, size - edge, edge + radius * 2], fill=images.ACCENT)
    return tile


def fitted_font(draw: ImageDraw.ImageDraw, text: str, width: int, size: int):
    """<summary>
    The largest label font, within reason, that draws ``text`` inside ``width``.
    </summary>
    <param name="draw">The canvas, asked how wide the text comes out.</param>
    <param name="text">The label to be drawn.</param>
    <param name="width">The room it has, in pixels.</param>
    <param name="size">One tile's size, which sets the largest font tried.</param>
    <returns>A Pillow font.</returns>
    <remarks>
    Measured rather than guessed from the character count, because the names
    here run from "Bell" to "Microphone Off" and a guess would either clip the
    long ones or shrink the short ones for nothing. The floor stops a very
    long name shrinking to something unreadable; it would overrun instead,
    which is the better failure and a visible prompt to shorten the name.
    </remarks>
    """
    for step in range(round(size * 0.18), round(size * 0.11), -1):
        font = images.load_font(step)
        if draw.textlength(text, font=font) <= width:
            return font
    return images.load_font(round(size * 0.11))


def render(chosen: list[dict], size: int, columns: int) -> Image.Image:
    """<summary>
    Lay the themes out one under the other and hand back the picture.
    </summary>
    <param name="chosen">Themes as <see cref="themes.list_themes"/> gives them.</param>
    <param name="size">One tile's size in pixels; everything else is a share of it.</param>
    <param name="columns">How many tiles to a row.</param>
    <returns>The finished sheet.</returns>
    <remarks>
    The height is worked out before anything is drawn rather than by growing
    the canvas, because a row is only as tall as a tile plus its label and the
    arithmetic is easier to check in one place than spread through the loop.
    </remarks>
    """
    gap = round(size * GAP)
    margin = round(size * MARGIN)
    label_height = round(size * LABEL_HEIGHT)
    heading_height = round(size * HEADING_HEIGHT)
    row_height = size + label_height + gap
    width = margin * 2 + columns * size + (columns - 1) * gap
    height = margin * 2
    for theme in chosen:
        rows = (len(theme["icons"]) + columns - 1) // columns
        height += heading_height + rows * row_height
    sheet = Image.new("RGB", (width, height), SURROUND)
    draw = ImageDraw.Draw(sheet)
    heading_font = images.load_font(round(size * 0.34))

    y = margin
    for theme in chosen:
        draw.text((margin, y + heading_height / 2), theme["name"], fill=images.FOREGROUND,
                  font=heading_font, anchor="lm")
        draw.text((width - margin, y + heading_height / 2), f"theme:{theme['slug']}/",
                  fill=images.ACCENT, font=heading_font, anchor="rm")
        y += heading_height
        folder = themes.THEMES_DIR / theme["slug"]
        for index, icon in enumerate(theme["icons"]):
            column = index % columns
            if column == 0 and index:
                y += row_height
            x = margin + column * (size + gap)
            tile = draw_icon(folder / icon["file"], size, icon["animated"])
            rounded = Image.new("RGB", (size, size), SURROUND)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                                  radius=round(size * CORNER), fill=255)
            rounded.paste(tile, (0, 0), mask)
            sheet.paste(rounded, (x, y))
            label = icon["label"]
            draw.text((x + size / 2, y + size + label_height / 2), label,
                      fill=images.FOREGROUND, font=fitted_font(draw, label, size + gap, size),
                      anchor="mm")
        y += row_height
    return sheet


def main() -> int:
    """<summary>
    Command line entry point: read the themes, draw the sheet, write a PNG.
    </summary>
    <returns>0, or a message through SystemExit for an unknown theme.</returns>
    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("</summary>")[0].replace("<summary>", "").strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--out", required=True, help="where to write the PNG")
    parser.add_argument("--theme", action="append", default=[], metavar="SLUG",
                        help="draw only this theme; may be given more than once")
    parser.add_argument("--size", type=int, default=110, help="one tile's size in pixels (default: 110)")
    parser.add_argument("--columns", type=int, default=9, help="tiles to a row (default: 9)")
    args = parser.parse_args()
    available = themes.list_themes()
    if args.theme:
        by_slug = {theme["slug"]: theme for theme in available}
        missing = [slug for slug in args.theme if slug not in by_slug]
        if missing:
            raise SystemExit(f"no theme called {', '.join(missing)}")
        available = [by_slug[slug] for slug in args.theme]
    if not available:
        raise SystemExit("no themes found")
    sheet = render(available, args.size, max(1, args.columns))
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"wrote {out} ({sheet.width} by {sheet.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
