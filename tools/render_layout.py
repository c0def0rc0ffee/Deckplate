#!/usr/bin/env python3
"""<summary>
Draw a picture of what a configuration looks like on the deck, for the
documentation.

    python tools/render_layout.py examples/stream-desk.toml -o docs/images/stream-desk.png
    python tools/render_layout.py examples/focus-timer.toml --page Focus --live 0,0=18:42@0.75

Renders one page as the fifteen keys and the three display panels, on the
deck's own dark surround, and writes it as a PNG.
</summary>
<remarks>
This is a documentation tool, not part of the program. It touches no
hardware, opens no device and imports nothing from the device or protocol
layers: it asks the tiles module for the same pictures the daemon would
draw and lays them out on a canvas. A deck does not need to be plugged in,
and nothing it produces can reach one.

The pictures are deterministic, so regenerating them after a change gives a
clean diff: the clock and the weather come from fixed values rather than
from the real time and the network, and every key that would show a live
value is drawn in the state it has before it is first pressed. ``--live``
and ``--active`` override that for a key or two, which is how a picture
shows a timer part way through rather than every key sitting idle.
</remarks>"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deckplate import config as cfg  # noqa: E402
from deckplate import images, layout, live, tiles  # noqa: E402
from deckplate.weather import Observation  # noqa: E402

# The surround the keys sit on, the space between them, and the wider gap
# before the display strip, all as a share of one key.
SURROUND = (16, 22, 32)
GAP = 0.09
STRIP_GAP = 0.3
MARGIN = 0.22
CORNER = 0.09
# What the clock, date and weather panels show, so a picture made today and
# one made next week are the same file.
MOMENT = datetime(2026, 9, 19, 18, 20)
WEATHER = Observation(temperature=12.0, code=3, label="Cloudy", icon="cloud", unit="°C")


def parse_live(text: str) -> tuple[tuple[int, int], live.Face]:
    """<summary>
    Read a ``row,column=TEXT`` or ``row,column=TEXT@RING`` override.
    </summary>
    <param name="text">One ``--live`` argument.</param>
    <returns>The position and the face to draw there.</returns>
    <exception cref="ValueError">The argument is not in that shape.</exception>"""
    match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*=\s*([^@]+?)\s*(?:@\s*([0-9.]+)\s*)?", text)
    if match is None:
        raise ValueError(f"'{text}' is not row,column=TEXT or row,column=TEXT@RING")
    row, column, value, ring = match.groups()
    return (int(row), int(column)), live.Face(text=value, ring=None if ring is None else float(ring), active=True)


def parse_position(text: str) -> tuple[int, int]:
    """<summary>Read a ``row,column`` argument.</summary>
    <param name="text">One ``--active`` argument.</param>
    <returns>The position.</returns>
    <exception cref="ValueError">The argument is not two numbers.</exception>"""
    match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", text)
    if match is None:
        raise ValueError(f"'{text}' is not row,column")
    return int(match.group(1)), int(match.group(2))


def idle_face(key) -> live.Face | None:
    """<summary>
    The face a key that remembers something shows before it is first pressed.
    </summary>
    <param name="key">The key's config, or None.</param>
    <returns>A face, or None for a key that shows no live value.</returns>
    <remarks>
    The same answers the controller gives for a state it has never seen: a
    timer shows its whole length with a full ring, a stopwatch 0:00, a
    counter 0. Kept here rather than imported because the controller's
    version needs a running controller, and this one needs to work from a
    file alone.
    </remarks>
    """
    if key is None:
        return None
    for action in key.actions():
        if action is None or action.params.get("reset"):
            continue
        if action.type == "timer":
            return live.Face(text=live.format_seconds(action.params["seconds"]), ring=1.0)
        if action.type == "stopwatch":
            return live.Face(text="0:00")
        if action.type == "counter":
            return live.Face(text="0")
    return None


def draw_key(key, size: int, face: live.Face | None, active: bool, background: str | None) -> Image.Image:
    """<summary>
    One key panel as the deck would show it.
    </summary>
    <param name="key">The key's config, or None for an empty position.</param>
    <param name="size">The panel size in pixels.</param>
    <param name="face">A live value to show, or None.</param>
    <param name="active">Draw the key's active picture and label instead.</param>
    <param name="background">The deck wide background colour.</param>
    <returns>An upright tile.</returns>
    <remarks>An animated key is drawn as its first frame, since a PNG cannot
    move. The ring colour is the accent, never the alert red: a picture of a
    finished timer would read as a fault rather than as a feature.</remarks>
    """
    from dataclasses import replace

    if key is not None and active and (key.image_active or key.label_active):
        key = replace(key, image=key.image_active or key.image, label=key.label_active or key.label)
    if face is not None and face.text:
        return tiles.live_tile(key, size, face.text, face.ring, images.ACCENT, default_background=background)
    animated = tiles.key_frames(key, size, default_background=background)
    if animated is not None:
        return animated[0][0]
    return tiles.key_tile(key, size, default_background=background)


def render(config: cfg.Config, page_name: str | None, size: int,
           faces: dict, actives: set) -> Image.Image:
    """<summary>
    Lay one page out as the deck shows it and hand back the picture.
    </summary>
    <param name="config">A loaded configuration.</param>
    <param name="page_name">Which page, or None for the first.</param>
    <param name="size">One key's size in pixels; everything else is a share of it.</param>
    <param name="faces">Position to face, overriding what a key would show.</param>
    <param name="actives">Positions to draw with their active face.</param>
    <returns>The finished picture.</returns>
    <exception cref="SystemExit">The named page is not in the config.</exception>"""
    page = config.pages[0]
    if page_name is not None:
        found = [p for p in config.pages if p.name == page_name]
        if not found:
            raise SystemExit(f"no page called '{page_name}' in this config")
        page = found[0]
    gap = round(size * GAP)
    strip_gap = round(size * STRIP_GAP)
    margin = round(size * MARGIN)
    width = margin * 2 + layout.LCD_COLUMNS * size + (layout.LCD_COLUMNS - 1) * gap + strip_gap + size
    height = margin * 2 + layout.ROWS * size + (layout.ROWS - 1) * gap
    sheet = Image.new("RGB", (width, height), SURROUND)
    draw = ImageDraw.Draw(sheet)
    draw.rounded_rectangle([1, 1, width - 2, height - 2], radius=round(size * CORNER),
                           outline=(38, 48, 62), width=2)
    for row in range(layout.ROWS):
        y = margin + row * (size + gap)
        for column in range(layout.LCD_COLUMNS):
            key = page.keys.get((row, column))
            face = faces.get((row, column), idle_face(key))
            tile = draw_key(key, size, face, (row, column) in actives, config.deck.background)
            sheet.paste(tile, (margin + column * (size + gap), y))
        panel = tiles.strip_tile(config.strip[row], MOMENT, WEATHER, size,
                                 config.weather.location_name or "", config.strip_visible,
                                 config.deck.background)
        sheet.paste(panel, (width - margin - size, y))
    return sheet


def main() -> int:
    """<summary>
    Command line entry point: read a config, draw a page, write a PNG.
    </summary>
    <returns>0, or a message through SystemExit for a bad page name.</returns>
    """
    parser = argparse.ArgumentParser(description=__doc__.split("</summary>")[0].replace("<summary>", "").strip(),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="the configuration file to draw")
    parser.add_argument("-o", "--out", required=True, help="where to write the PNG")
    parser.add_argument("--page", help="which page to draw (default: the first)")
    parser.add_argument("--size", type=int, default=150, help="one key's size in pixels (default: 150)")
    parser.add_argument("--live", action="append", default=[], metavar="R,C=TEXT[@RING]",
                        help="show a live value on a key, such as 1,2=4:12@0.8")
    parser.add_argument("--active", action="append", default=[], metavar="R,C",
                        help="draw a key with its active picture and label")
    args = parser.parse_args()
    config = cfg.load(Path(args.config).expanduser())
    faces = dict(parse_live(one) for one in args.live)
    actives = {parse_position(one) for one in args.active}
    sheet = render(config, args.page, args.size, faces, actives)
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"wrote {out} ({sheet.width} by {sheet.height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
