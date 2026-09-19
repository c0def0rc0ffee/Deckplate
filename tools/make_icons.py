#!/usr/bin/env python3
"""<summary>
Draw a set of key pictures for space sim style actions and write them as PNGs.

    python tools/make_icons.py ~/.config/deckplate/images
    python tools/make_icons.py --list

Writes sc-<name>.png files into the folder given, overwriting same named
ones. Safe to run anywhere; touches nothing but that folder.
</summary>
<remarks>
Flat pictograms on a transparent background in the page's own palette, so a
key's background colour shows through and a label's band sits under them. Each
is drawn at four times the output size and scaled down, so the strokes are
smooth on the deck at 95 pixels and in the editor at three times that.

Nothing here copies any game's artwork: they are plain glyphs for the things a
cockpit needs, hangar, landing gear, quantum drive and so on. Keep it that way.
A traced logo or a lifted icon would make the shipped theme unredistributable,
and the whole point of drawing them in code is that their provenance is the
code.

This is a generator, not part of the program. It touches no hardware, sends
nothing to a deck and knows nothing about the protocol: it writes image files
and stops. The program reads those files later like any other picture.

Two output shapes, and the difference matters. A plain folder argument writes
``sc-<name>.png`` files, which is the form a user's own image folder takes. The
``--theme`` option writes bare ``<name>.png`` files plus a manifest, which is
the form ``deckplate/themes/<slug>/`` takes so the picker can offer the set as
one theme. Write the wrong one into the wrong place and the picker shows
nothing, with no error.
</remarks>"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 285          # three times the deck's 95 px key, the editor's largest preview
SCALE = 4           # drawn at this multiple of SIZE, then shrunk
UNITS = 100.0       # icons are drawn on a 0 to 100 grid
STROKE = 8.0        # line weight in units, about 7 px on the deck

INK = (232, 237, 242, 255)      # the page's text colour
SIGNAL = (92, 155, 222, 255)    # the page's blue
STEEL = (143, 160, 175, 255)    # the page's muted grey
WARM = (255, 200, 60, 255)      # flame and warnings
RED = (232, 96, 96, 255)


class Pen:
    """<summary>
    Drawing helpers on the unit grid, so each icon reads as geometry.
    </summary>
    <remarks>
    One Pen is one icon. It owns the oversized image it draws into, so a Pen is
    used once, handed to a single icon function and then asked for its result.
    Reusing one draws two glyphs on top of each other.

    Every coordinate an icon gives is in the 0 to 100 grid, never in pixels.
    The Pen multiplies on the way to Pillow, which is what lets the same
    geometry render at any size, so an icon that reaches for a pixel value has
    broken the arrangement.

    Widths are in grid units too and are rounded to at least one pixel, so a
    hairline never disappears at the smallest size.
    </remarks>
    """

    def __init__(self) -> None:
        """
        <summary>
        A fresh transparent canvas at SIZE times SCALE with its draw handle, and the pixel size of one unit.
        </summary>
        """
        self.px = SIZE * SCALE / UNITS
        self.image = Image.new("RGBA", (SIZE * SCALE, SIZE * SCALE), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)

    def p(self, x: float, y: float) -> tuple[float, float]:
        """
        <summary>
        Unit coordinates to pixels.
        </summary>
        <param name="x">Units.</param>
        <param name="y">Units.</param>
        <returns>(x, y) in pixels.</returns>
        """
        return x * self.px, y * self.px

    def w(self, units: float = STROKE) -> int:
        """
        <summary>
        A stroke width in pixels, at least 1.
        </summary>
        <param name="units">Width in units.</param>
        <returns>An int.</returns>
        """
        return max(1, round(units * self.px))

    def line(self, points: list[tuple[float, float]], colour=INK, width: float = STROKE) -> None:
        """<summary>
        Draw a polyline with rounded joints and rounded ends.
        </summary>
        <param name="points">Two or more grid points, in order.</param>
        <param name="colour">RGBA tuple, normally one of the palette
        constants.</param>
        <param name="width">Stroke weight in grid units.</param>
        <remarks>
        Pillow draws square ends and mitred joints, which look wrong on a
        pictogram at 95 pixels, so a filled circle is stamped at every point to
        round the line off by hand. That is why this exists rather than the
        draw call being used directly.
        </remarks>
        """
        pts = [self.p(x, y) for x, y in points]
        self.draw.line(pts, fill=colour, width=self.w(width), joint="curve")
        r = self.w(width) / 2
        for x, y in pts:
            self.draw.ellipse([x - r, y - r, x + r, y + r], fill=colour)

    def circle(self, cx: float, cy: float, r: float, colour=INK, width: float | None = STROKE, fill=None) -> None:
        """<summary>
        Draw a circle by centre and radius, filled, outlined or both.
        </summary>
        <param name="cx">Centre across, in grid units.</param>
        <param name="cy">Centre down, in grid units.</param>
        <param name="r">Radius in grid units, not a diameter.</param>
        <param name="width">Outline weight, or None for no outline at all.
        Passing None with no fill draws nothing.</param>
        <param name="fill">Interior colour, or None to leave it clear.</param>
        <remarks>
        Centre and radius rather than the bounding box Pillow wants, because
        every icon here is laid out from its centre.
        </remarks>
        """
        box = [*self.p(cx - r, cy - r), *self.p(cx + r, cy + r)]
        if fill is not None:
            self.draw.ellipse(box, fill=fill)
        if width:
            self.draw.ellipse(box, outline=colour, width=self.w(width))

    def arc(self, cx: float, cy: float, r: float, start: float, end: float, colour=INK, width: float = STROKE) -> None:
        """<summary>
        Draw part of a circle, from ``start`` to ``end`` in degrees.
        </summary>
        <param name="start">Opening angle in degrees.</param>
        <param name="end">Closing angle, swept clockwise from the
        start.</param>
        <remarks>
        Angles follow Pillow: zero is at three o'clock and they increase
        clockwise, because the vertical axis of the image points down. So the
        upper half of a circle is 180 to 360, which reads backwards until you
        remember that.

        Ends are square here, unlike <see cref="line"/>, so use this for a
        sweep that meets something else rather than for a stroke that ends in
        mid air.
        </remarks>
        """
        box = [*self.p(cx - r, cy - r), *self.p(cx + r, cy + r)]
        self.draw.arc(box, start, end, fill=colour, width=self.w(width))

    def polygon(self, points: list[tuple[float, float]], fill=None, colour=INK, width: float | None = STROKE) -> None:
        """<summary>
        Draw a closed shape, filled, outlined with rounded joints, or both.
        </summary>
        <param name="points">The corners in order. The shape is closed for
        you, so do not repeat the first point.</param>
        <param name="width">Outline weight, or None for a fill with no outline,
        which is what the solid glyphs use.</param>
        <remarks>
        The outline goes through <see cref="line"/> rather than Pillow's own
        polygon outline, so the corners are rounded to match every other stroke
        in the set.

        An outline is centred on the edge, so an outlined shape is about one
        stroke wider than the points say. Allow for it when a glyph has to sit
        inside the 12 to 88 band.
        </remarks>
        """
        pts = [self.p(x, y) for x, y in points]
        if fill is not None:
            self.draw.polygon(pts, fill=fill)
        if width:
            self.line(points + [points[0]], colour, width)

    def rounded(self, x0: float, y0: float, x1: float, y1: float, radius: float, colour=INK,
                width: float | None = STROKE, fill=None) -> None:
        """<summary>
        Draw a rounded rectangle from two opposite corners.
        </summary>
        <param name="x0">Left edge, in grid units.</param>
        <param name="y0">Top edge, in grid units.</param>
        <param name="x1">Right edge. Must be greater than ``x0``.</param>
        <param name="y1">Bottom edge. Must be greater than ``y0``.</param>
        <param name="radius">Corner radius in grid units.</param>
        <param name="width">Outline weight, or None for no outline.</param>
        <remarks>
        Corners given the wrong way round make Pillow raise rather than drawing
        the rectangle it could have inferred, so keep the smaller value first.
        </remarks>
        """
        box = [*self.p(x0, y0), *self.p(x1, y1)]
        self.draw.rounded_rectangle(box, radius=radius * self.px, fill=fill, outline=colour if width else None,
                                    width=self.w(width) if width else 0)

    def dot(self, cx: float, cy: float, r: float, colour=INK) -> None:
        """
        <summary>
        A filled circle.
        </summary>
        <param name="cx">Centre x.</param>
        <param name="cy">Centre y.</param>
        <param name="r">Radius.</param>
        <param name="colour">Fill.</param>
        """
        self.circle(cx, cy, r, colour, None, fill=colour)

    def result(self) -> Image.Image:
        """<summary>
        Shrink the oversized drawing down to the output size and hand it back.
        </summary>
        <returns>A SIZE square RGBA image with a transparent background.</returns>
        <remarks>
        The shrink is where the smooth edges come from: everything above is
        drawn hard edged at four times the size. Call it once, at the end, and
        do not draw into the Pen afterwards.

        The Pen keeps its own full size image, so this does not consume it, but
        calling it twice just does the same work again.
        </remarks>
        """
        return self.image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


# Each icon is a function of a Pen. Keep the glyph within roughly 12 to 88 on
# both axes, biased a little high so a label band along the bottom does not
# swallow it.

def hangar(pen: Pen) -> None:
    """<summary>
    A wide bay roof over a lit doorway, with a down arrow above it: the
    request to land and be taken inside.
    </summary>"""
    pen.line([(14, 70), (14, 42), (50, 18), (86, 42), (86, 70)])
    pen.line([(14, 70), (86, 70)])
    pen.rounded(36, 48, 64, 70, 3, SIGNAL, None, fill=SIGNAL)
    pen.line([(50, 26), (50, 42)], INK, 5)
    pen.line([(43, 36), (50, 43), (57, 36)], INK, 5)


def landing_gear(pen: Pen) -> None:
    """<summary>
    A strut coming down from the hull to a single lit wheel, with a brace
    behind it: the gear lowered.
    </summary>
    <remarks>
    Deliberately the gear alone, so it reads at a glance against
    <see cref="landing_lock"/>, which adds a padlock beside a smaller copy of
    the same shape.
    </remarks>"""
    pen.line([(30, 18), (70, 18)])
    pen.line([(50, 18), (50, 52)])
    pen.line([(50, 36), (32, 52)], STEEL, 6)
    pen.circle(50, 66, 14, INK, None, fill=INK)
    pen.circle(50, 66, 6, SIGNAL, None, fill=SIGNAL)


def quantum(pen: Pen) -> None:
    """<summary>
    An arrow pointing right with three speed lines trailing it: engage the
    quantum drive and travel.
    </summary>"""
    pen.line([(30, 50), (82, 50)], SIGNAL)
    pen.line([(62, 30), (82, 50), (62, 70)], SIGNAL)
    pen.line([(14, 36), (30, 36)], STEEL, 6)
    pen.line([(10, 50), (22, 50)], STEEL, 6)
    pen.line([(14, 64), (30, 64)], STEEL, 6)


def power(pen: Pen) -> None:
    """<summary>
    The standard power symbol, a broken ring with a stroke through the gap.
    </summary>
    <remarks>
    The ring is an arc rather than a circle with a hole cut in it, so the gap
    stays open at every size instead of closing up when the stroke is rounded
    to whole pixels.
    </remarks>"""
    pen.arc(50, 52, 30, -60, 240, INK)
    pen.line([(50, 14), (50, 48)], SIGNAL)


def engines(pen: Pen) -> None:
    """<summary>
    A thruster bell with a flame beneath it, warm outside and red at the core.
    </summary>"""
    pen.polygon([(36, 16), (64, 16), (72, 46), (28, 46)], fill=None, colour=INK)
    pen.polygon([(36, 50), (64, 50), (50, 86)], fill=WARM, colour=WARM, width=None)
    pen.polygon([(44, 50), (56, 50), (50, 70)], fill=RED, colour=RED, width=None)


def shields(pen: Pen) -> None:
    """<summary>
    A six sided shield with a smaller lit shield held inside it: shields up.
    </summary>"""
    pen.line([(50, 14), (80, 24), (80, 48), (50, 84), (20, 48), (20, 24), (50, 14)], INK)
    pen.line([(50, 34), (64, 40), (64, 50), (50, 66), (36, 50), (36, 40), (50, 34)], SIGNAL, 5)


def weapons(pen: Pen) -> None:
    """<summary>
    A gunsight: a ring with four ticks outside it and a red dot at the centre.
    </summary>"""
    pen.circle(50, 50, 28, INK)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        pen.line([(50 + dx * 20, 50 + dy * 20), (50 + dx * 38, 50 + dy * 38)], INK)
    pen.dot(50, 50, 5, RED)


def missiles(pen: Pen) -> None:
    """<summary>
    A missile seen from behind, fins either side and a flame below, with a lit
    seeker head: missiles armed.
    </summary>"""
    pen.polygon([(50, 10), (62, 30), (62, 62), (38, 62), (38, 30)], fill=INK, colour=INK, width=None)
    pen.polygon([(38, 48), (24, 66), (38, 62)], fill=STEEL, colour=STEEL, width=None)
    pen.polygon([(62, 48), (76, 66), (62, 62)], fill=STEEL, colour=STEEL, width=None)
    pen.polygon([(42, 64), (58, 64), (50, 86)], fill=WARM, colour=WARM, width=None)
    pen.dot(50, 30, 5, SIGNAL)


def scan(pen: Pen) -> None:
    """<summary>
    A radar sweep, two arcs over a horizon with a lit sweep arm and a contact
    off to one side.
    </summary>
    <remarks>
    One shot of the sweep. <see cref="scan_loop"/> is the repeating version and
    the two have to stay distinguishable on neighbouring keys.
    </remarks>"""
    pen.arc(50, 56, 34, 180, 360, STEEL, 5)
    pen.arc(50, 56, 22, 180, 360, STEEL, 5)
    pen.line([(16, 56), (84, 56)], INK, 5)
    pen.line([(50, 56), (76, 30)], SIGNAL)
    pen.dot(50, 56, 5, INK)
    pen.dot(30, 44, 4, SIGNAL)


def starmap(pen: Pen) -> None:
    """<summary>
    A planet with an orbit arc behind it and a small star above: the star map.
    </summary>"""
    pen.circle(46, 54, 22, INK)
    pen.arc(46, 54, 34, 160, 340, STEEL, 5)
    star = [(74 + 8 * math.cos(math.radians(-90 + i * 36)) * (1 if i % 2 == 0 else 0.45),
             26 + 8 * math.sin(math.radians(-90 + i * 36)) * (1 if i % 2 == 0 else 0.45)) for i in range(10)]
    pen.polygon(star, fill=WARM, colour=WARM, width=None)


def mobiglas(pen: Pen) -> None:
    """<summary>
    A wrist display: a rounded panel with three lines of text on it and a strap
    out to either side.
    </summary>"""
    pen.rounded(22, 24, 78, 76, 8, INK, STROKE)
    pen.line([(34, 40), (66, 40)], SIGNAL, 5)
    pen.line([(34, 52), (58, 52)], STEEL, 5)
    pen.line([(34, 64), (50, 64)], STEEL, 5)
    pen.line([(22, 50), (12, 50)], STEEL, 6)
    pen.line([(78, 50), (88, 50)], STEEL, 6)


def comms(pen: Pen) -> None:
    """<summary>
    A speech bubble with three lit dots in it: open communications.
    </summary>"""
    pen.rounded(14, 20, 86, 68, 12, INK, STROKE)
    pen.polygon([(30, 66), (30, 84), (48, 66)], fill=INK, colour=INK, width=None)
    for x in (36, 50, 64):
        pen.dot(x, 44, 4, SIGNAL)


def cargo(pen: Pen) -> None:
    """<summary>
    A crate drawn as an open box in three quarter view, with a lit strap across
    one face: the cargo hold.
    </summary>"""
    pen.line([(50, 14), (82, 30), (82, 66), (50, 82), (18, 66), (18, 30), (50, 14)], INK)
    pen.line([(18, 30), (50, 46), (82, 30)], INK, 5)
    pen.line([(50, 46), (50, 82)], INK, 5)
    pen.line([(30, 24), (62, 40)], SIGNAL, 4)


def mining(pen: Pen) -> None:
    """<summary>
    A cut gem with a mining beam coming down onto it from above.
    </summary>"""
    pen.polygon([(50, 70), (28, 50), (40, 34), (60, 34), (72, 50)], fill=None, colour=INK)
    pen.line([(40, 34), (50, 70), (60, 34)], STEEL, 4)
    pen.line([(50, 30), (50, 12)], SIGNAL)
    pen.line([(38, 22), (30, 14)], SIGNAL, 5)
    pen.line([(62, 22), (70, 14)], SIGNAL, 5)


def medical(pen: Pen) -> None:
    """<summary>
    A red cross on a rounded panel: the medical or first aid action.
    </summary>"""
    pen.rounded(18, 18, 82, 82, 12, INK, STROKE)
    pen.line([(50, 32), (50, 68)], RED, 10)
    pen.line([(32, 50), (68, 50)], RED, 10)


def exit_seat(pen: Pen) -> None:
    """<summary>
    A doorway with an arrow leaving through it: get out of the seat.
    </summary>
    <remarks>
    The function is named for the seat because ``exit`` is a builtin. The icon
    is registered under the plain name "exit", which is what the key and the
    file are called, so the two spellings are not a mistake to tidy away.
    </remarks>"""
    pen.line([(46, 16), (18, 16), (18, 84), (46, 84)], INK)
    pen.line([(38, 50), (82, 50)], SIGNAL)
    pen.line([(66, 34), (82, 50), (66, 66)], SIGNAL)


def lights(pen: Pen) -> None:
    """<summary>
    A lamp throwing four warm beams to the right: the exterior lights.
    </summary>"""
    pen.circle(38, 52, 14, INK, None, fill=INK)
    pen.circle(38, 52, 14, INK)
    for angle in (-30, -10, 10, 30):
        a = math.radians(angle)
        pen.line([(56 + 4 * math.cos(a), 52 + 4 * math.sin(a)), (56 + 30 * math.cos(a), 52 + 30 * math.sin(a))], WARM, 5)


def cruise(pen: Pen) -> None:
    """<summary>
    Three nested chevrons pointing right, the leading one lit: cruise control
    or speed limiter.
    </summary>"""
    for x, colour in ((18, STEEL), (38, INK), (58, SIGNAL)):
        pen.line([(x, 26), (x + 22, 50), (x, 74)], colour)


def decoupled(pen: Pen) -> None:
    """<summary>
    Two panels side by side with the link between them broken: decoupled
    flight, where the ship no longer holds its own heading.
    </summary>"""
    pen.rounded(14, 32, 42, 68, 6, INK, STROKE)
    pen.rounded(58, 32, 86, 68, 6, SIGNAL, STROKE)
    pen.line([(44, 50), (50, 50)], STEEL, 5)
    pen.line([(53, 50), (56, 50)], STEEL, 5)


def inventory(pen: Pen) -> None:
    """<summary>
    A bag or case with a handle and a lit label on the front: the inventory.
    </summary>"""
    pen.rounded(22, 30, 78, 84, 10, INK, STROKE)
    pen.arc(50, 30, 14, 180, 360, INK)
    pen.line([(36, 30), (36, 20)], INK)
    pen.line([(64, 30), (64, 20)], INK)
    pen.rounded(38, 50, 62, 66, 4, SIGNAL, None, fill=SIGNAL)


def helmet(pen: Pen) -> None:
    """<summary>
    A helmet in profile with a lit visor and a collar under it: put the suit
    helmet on or take it off.
    </summary>"""
    pen.circle(50, 48, 32, INK, STROKE)
    pen.rounded(26, 44, 74, 62, 9, SIGNAL, None, fill=SIGNAL)
    pen.line([(24, 74), (76, 74)], STEEL, 7)


def scan_loop(pen: Pen) -> None:
    """<summary>
    The scan sweep with an arrow looping over it: scanning again and again.
    </summary>
    <remarks>
    The looping arrow is what separates this from <see cref="scan"/>, so the
    two have to stay visibly different at 95 pixels.
    </remarks>
    """
    pen.arc(50, 62, 24, 180, 360, STEEL, 5)
    pen.line([(26, 62), (74, 62)], INK, 5)
    pen.line([(50, 62), (67, 45)], SIGNAL)
    pen.dot(50, 62, 4, INK)
    pen.arc(50, 48, 34, 190, 350, SIGNAL, 6)
    pen.polygon([(77, 33), (90, 42), (75, 50)], fill=SIGNAL, colour=SIGNAL, width=None)


def landing_lock(pen: Pen) -> None:
    """<summary>
    Landing gear with a padlock: the gear held down where it is.
    </summary>
    <remarks>
    The gear is pushed left of centre to leave room for the padlock, so it is
    not the same geometry as <see cref="landing_gear"/> and the two are not
    interchangeable.
    </remarks>
    """
    pen.line([(20, 20), (52, 20)])
    pen.line([(36, 20), (36, 46)])
    pen.circle(36, 58, 12, INK, None, fill=INK)
    pen.circle(36, 58, 5, SIGNAL, None, fill=SIGNAL)
    pen.arc(72, 60, 11, 180, 360, WARM, 5)
    pen.rounded(60, 60, 84, 82, 4, WARM, None, fill=WARM)


def view_change(pen: Pen) -> None:
    """<summary>
    A screen with an arrow above and below: cycle to the next camera view.
    </summary>
    <remarks>
    The arrows point opposite ways deliberately, for a cycle that goes both
    directions rather than a single step forward.
    </remarks>
    """
    pen.rounded(22, 30, 78, 72, 8, INK, STROKE)
    pen.circle(50, 51, 10, SIGNAL, 6)
    pen.line([(30, 20), (56, 20)], SIGNAL, 5)
    pen.line([(50, 14), (58, 20), (50, 26)], SIGNAL, 5)
    pen.line([(70, 82), (44, 82)], SIGNAL, 5)
    pen.line([(50, 76), (42, 82), (50, 88)], SIGNAL, 5)


ICONS = {
    "hangar": hangar, "landing-gear": landing_gear, "quantum": quantum, "power": power,
    "engines": engines, "shields": shields, "weapons": weapons, "missiles": missiles,
    "scan": scan, "starmap": starmap, "mobiglas": mobiglas, "comms": comms,
    "cargo": cargo, "mining": mining, "medical": medical, "exit": exit_seat,
    "lights": lights, "cruise": cruise, "decoupled": decoupled, "inventory": inventory,
    "helmet": helmet, "scan-loop": scan_loop, "landing-lock": landing_lock,
    "view-change": view_change,
}

# The theme these icons make up, and the label shown under each in the picker.
THEME_NAME = "Space Game"
LABELS = {
    "hangar": "Hangar", "landing-gear": "Landing Gear", "quantum": "Quantum",
    "power": "Power", "engines": "Engines", "shields": "Shields", "weapons": "Weapons",
    "missiles": "Missiles", "scan": "Scan", "starmap": "Star Map", "mobiglas": "mobiGlas",
    "comms": "Comms", "cargo": "Cargo", "mining": "Mining", "medical": "Medical",
    "exit": "Exit", "lights": "Lights", "cruise": "Cruise", "decoupled": "Decoupled",
    "inventory": "Inventory", "helmet": "Helmet", "scan-loop": "Scan Loop",
    "landing-lock": "Landing Lock", "view-change": "View Change",
}


def write_theme(folder: Path) -> int:
    """<summary>
    Write the icons as a shipped theme: bare id.png files and a manifest.json.
    </summary>
    <param name="folder">Where to write. It is created if missing, and same
    named files in it are overwritten.</param>
    <returns>How many icons were written, not counting the manifest.</returns>
    <remarks>
    This is the form deckplate/themes/ holds a theme in, so the picker can
    offer the set as a theme instead of it living among the user's own uploads.
    The file names carry no prefix here, unlike the user folder output, and the
    manifest is what supplies the human readable labels.

    Every icon in <see cref="ICONS"/> must have an entry in
    <see cref="LABELS"/> or this raises part way through, leaving a folder of
    images with no manifest beside them.
    </remarks>
    """
    import json
    folder.mkdir(parents=True, exist_ok=True)
    for name in ICONS:
        render(name).save(folder / f"{name}.png")
    manifest = {"name": THEME_NAME, "icons": [{"id": name, "label": LABELS[name]} for name in ICONS]}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return len(ICONS)


def render(name: str) -> Image.Image:
    """<summary>
    Draw one named icon on a fresh Pen and return the finished image.
    </summary>
    <param name="name">A key of <see cref="ICONS"/>.</param>
    <returns>One icon as a SIZE square RGBA image, background transparent.</returns>
    <remarks>
    A new Pen every time, so two calls never share a canvas. Nothing is cached:
    rendering the whole set is a few seconds and the tool runs rarely.
    </remarks>
    
    <exception cref="KeyError">There is no icon by that name.</exception>"""
    pen = Pen()
    ICONS[name](pen)
    return pen.result()



def help_text(doc: str | None) -> str:
    """<summary>
    Strip the XML doc tags out of a module docstring so it can be shown as
    command line help.
    </summary>
    <param name="doc">A module docstring in the house comment style, or None.</param>
    <returns>The prose alone, with the tag lines removed.</returns>
    <remarks>
    Every file in this project carries a tagged header block, and argparse
    prints whatever description it is handed verbatim. Passing the raw
    docstring therefore shows tags to the user, which is what this exists to
    prevent. Only lines that are nothing but a tag are dropped, so prose and
    the indented examples inside the block survive untouched. A cross
    reference written inline in a sentence is unwrapped to the bare name
    rather than dropped, because deleting the line would take the sentence
    around it with it.
    </remarks>
    """
    if not doc:
        return ""
    tag = re.compile(r"^\s*</?(?:summary|remarks|param|returns|exception|see|seealso)\b[^>]*>\s*$")
    ref = re.compile(r"<(?:see|seealso)\s+cref=\"([^\"]+)\"\s*/?>")
    kept = (ref.sub(r"\1", line) for line in doc.splitlines() if not tag.match(line))
    return "\n".join(kept).strip("\n")

def main() -> int:
    """<summary>
    Command line entry point: list the icons, write a theme, or write a folder
    of user images.
    </summary>
    <returns>0. Bad arguments leave through argparse rather than through a
    status returned from here.</returns>
    <remarks>
    The three modes are checked in order and the first one wins: ``--list``,
    then ``--theme``, then a folder argument. Giving both ``--theme`` and a
    folder is not an error and writes only the theme, which surprises people.

    ``--sheet`` applies to the folder mode alone. It writes one contact sheet of
    every icon at deck size on the page's dark background, which is the quick
    way to see whether a new glyph reads at 95 pixels before it goes anywhere
    near a key.
    </remarks>
    """
    parser = argparse.ArgumentParser(description=help_text(__doc__), formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", nargs="?", help="where to write the sc-<name>.png files")
    parser.add_argument("--list", action="store_true", help="print the icon names and stop")
    parser.add_argument("--theme", metavar="DIR",
                        help="write a shipped theme (bare <id>.png plus manifest.json) into DIR")
    parser.add_argument("--sheet", help="also write a contact sheet PNG to this path")
    args = parser.parse_args()
    if args.list:
        print("\n".join(ICONS))
        return 0
    if args.theme:
        folder = Path(args.theme).expanduser()
        count = write_theme(folder)
        print(f"wrote theme '{THEME_NAME}' ({count} icons and a manifest) into {folder}")
        return 0
    if not args.folder:
        parser.error("give a folder to write into, --theme DIR, or --list")
    folder = Path(args.folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    rendered = {}
    for name in ICONS:
        rendered[name] = render(name)
        rendered[name].save(folder / f"sc-{name}.png")
    print(f"wrote {len(rendered)} icons into {folder}")
    if args.sheet:
        columns = 5
        rows = math.ceil(len(rendered) / columns)
        cell = 95 + 12
        sheet = Image.new("RGB", (columns * cell + 12, rows * (cell + 14) + 12), (20, 28, 40))
        draw = ImageDraw.Draw(sheet)
        for index, (name, icon) in enumerate(rendered.items()):
            x = 12 + (index % columns) * cell
            y = 12 + (index // columns) * (cell + 14)
            small = icon.resize((95, 95), Image.Resampling.LANCZOS)
            sheet.paste(small, (x, y), small)
            draw.text((x + 2, y + 96), name, fill=(143, 160, 175))
        sheet.save(args.sheet)
        print(f"contact sheet at {args.sheet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
