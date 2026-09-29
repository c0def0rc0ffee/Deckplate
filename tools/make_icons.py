#!/usr/bin/env python3
"""<summary>
Draw the shipped icon sets and write them as PNGs, and animated GIFs where
an icon moves.

    python tools/make_icons.py --theme deckplate/themes/space-game
    python tools/make_icons.py --set controls --theme deckplate/themes/controls
    python tools/make_icons.py --list

Twelve sets: "space-game", the space sim glyphs; "controls", the pictures
for the deck's own actions (volume, windows, timers, toggles, media);
"desktop", the standard Linux desktop things (terminal, files, lock, power,
workspaces and so on); "sherbet" and "lagoon", the controls and desktop
glyphs again, repainted in warm and cool colour sweeps to sit beside a bright
gradient desktop icon theme; and six game flavoured sets, each with its own
finish: "grimdark" (gothic far future war), "wayfarer" (friendly planet
hopping), "field-command" (a modern strategy war), "rift" (alien crystal and
bio tech), "deep-colony" (marines in the dark) and "neon-chrome" (neon street
tech). None of them uses a real game's name or artwork. Last,
"amber-console": the controls and desktop glyphs plus a console set, drawn
as pixel art on an old amber on black terminal, each with a lit inverse
video face as well. A
plain folder argument writes sc-<name>.png files of the chosen set into it,
overwriting same named ones. Safe to run anywhere; touches nothing but that
folder.
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
import zlib
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

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

    def ellipse(self, x0: float, y0: float, x1: float, y1: float, colour=INK,
                width: float | None = STROKE, fill=None, start: float | None = None,
                end: float | None = None) -> None:
        """<summary>
        Draw an ellipse by its bounding box, or part of its outline.
        </summary>
        <param name="x0">Left edge, in grid units.</param>
        <param name="y0">Top edge.</param>
        <param name="x1">Right edge.</param>
        <param name="y1">Bottom edge.</param>
        <param name="width">Outline weight, or None for a fill alone.</param>
        <param name="fill">Interior colour, or None to leave it clear.</param>
        <param name="start">With ``end``, draw only the outline between these
        angles, in Pillow's clockwise degrees, and ignore the fill.</param>
        <param name="end">The closing angle of that part.</param>
        <remarks>
        Where <see cref="circle"/> is laid out from a centre, this takes a box,
        because the ellipses in the sets are rims, rings and shadows seen at an
        angle and are easiest to place by their edges.
        </remarks>
        """
        box = [*self.p(x0, y0), *self.p(x1, y1)]
        if start is not None and end is not None:
            self.draw.arc(box, start, end, fill=colour, width=self.w(width or STROKE))
            return
        if fill is not None:
            self.draw.ellipse(box, fill=fill)
        if width:
            self.draw.ellipse(box, outline=colour, width=self.w(width))

    def pie(self, x0: float, y0: float, x1: float, y1: float, start: float, end: float, colour=INK) -> None:
        """<summary>
        Fill a slice of an ellipse, from ``start`` to ``end`` degrees clockwise.
        </summary>
        <param name="x0">Left edge of the whole ellipse, in grid units.</param>
        <param name="y0">Top edge.</param>
        <param name="x1">Right edge.</param>
        <param name="y1">Bottom edge.</param>
        <param name="start">Opening angle, Pillow's degrees.</param>
        <param name="end">Closing angle.</param>
        <param name="colour">The fill.</param>
        <remarks>
        180 to 360 is the top half, which is what every dome and helmet uses.
        </remarks>
        """
        self.draw.pieslice([*self.p(x0, y0), *self.p(x1, y1)], start, end, fill=colour)

    def result(self, style=None, name: str = "") -> Image.Image:
        """<summary>
        Shrink the oversized drawing down to the output size and hand it back.
        </summary>
        <param name="style">A <see cref="Sweet"/> applied to the full size
        drawing before the shrink, or None for the flat palette.</param>
        <param name="name">The icon's id, passed to the style.</param>
        <returns>A SIZE square RGBA image with a transparent background.</returns>
        <remarks>
        The shrink is where the smooth edges come from: everything above is
        drawn hard edged at four times the size. Call it once, at the end, and
        do not draw into the Pen afterwards.

        The Pen keeps its own full size image, so this does not consume it, but
        calling it twice just does the same work again.
        </remarks>
        """
        image = style(self.image, name) if style else self.image
        return image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


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


# ---- The controls set: pictures for the deck's own actions -------------------

def _speaker(pen: Pen, x: float = 22) -> None:
    """<summary>The loudspeaker cone every volume glyph starts from.</summary>
    <param name="x">Where the back of the cone sits, so the glyph can shift left.</param>"""
    pen.polygon([(x, 40), (x + 14, 40), (x + 30, 24), (x + 30, 76), (x + 14, 60), (x, 60)], fill=INK, width=None)


def volume_up(pen: Pen) -> None:
    """<summary>A speaker with two sound waves: louder.</summary>"""
    _speaker(pen)
    pen.arc(56, 50, 12, -50, 50, SIGNAL)
    pen.arc(56, 50, 24, -50, 50, SIGNAL)


def volume_down(pen: Pen) -> None:
    """<summary>A speaker with one small wave: quieter.</summary>"""
    _speaker(pen)
    pen.arc(56, 50, 12, -50, 50, STEEL)


def mute(pen: Pen) -> None:
    """<summary>A speaker with a cross where the waves would be: muted.</summary>"""
    _speaker(pen)
    pen.line([(62, 38), (84, 62)], RED)
    pen.line([(84, 38), (62, 62)], RED)


def speaker(pen: Pen) -> None:
    """<summary>A loudspeaker cabinet with its driver: the speakers as an output.</summary>"""
    pen.rounded(28, 14, 72, 86, 6)
    pen.circle(50, 58, 14, INK, None, fill=INK)
    pen.circle(50, 58, 5, SIGNAL, None, fill=SIGNAL)
    pen.dot(50, 28, 5, STEEL)


def headphones(pen: Pen) -> None:
    """<summary>A headband over two ear cups: the headphones as an output.</summary>"""
    pen.arc(50, 58, 32, 180, 360, INK)
    pen.rounded(14, 54, 30, 80, 5, INK, None, fill=INK)
    pen.rounded(70, 54, 86, 80, 5, INK, None, fill=INK)
    pen.dot(22, 67, 4, SIGNAL)
    pen.dot(78, 67, 4, SIGNAL)


def cycle(pen: Pen) -> None:
    """<summary>Two arrows chasing round a circle: the next one along.</summary>"""
    pen.arc(50, 50, 28, 200, 340, SIGNAL)
    pen.arc(50, 50, 28, 20, 160, SIGNAL)
    pen.line([(76, 30), (78, 44), (66, 40)], SIGNAL, 6)
    pen.line([(24, 70), (22, 56), (34, 60)], SIGNAL, 6)


def _window(pen: Pen) -> None:
    """<summary>A window frame with its title bar, shared by the window glyphs.</summary>"""
    pen.rounded(14, 22, 86, 78, 5)
    pen.line([(14, 36), (86, 36)])
    pen.dot(23, 29, 3, SIGNAL)
    pen.dot(33, 29, 3, STEEL)


def window(pen: Pen) -> None:
    """<summary>A window brought to the front, with an arrow rising into it.</summary>"""
    _window(pen)
    pen.line([(50, 70), (50, 46)], SIGNAL, 6)
    pen.line([(40, 56), (50, 46), (60, 56)], SIGNAL, 6)


def window_minimise(pen: Pen) -> None:
    """<summary>A window with only a bar left: minimised.</summary>"""
    _window(pen)
    pen.line([(34, 64), (66, 64)], SIGNAL)


def window_maximise(pen: Pen) -> None:
    """<summary>A window with a larger frame ghosted behind it: maximised.</summary>"""
    _window(pen)
    pen.rounded(34, 46, 66, 68, 3, SIGNAL, 6)


def window_close(pen: Pen) -> None:
    """<summary>A window with a cross in it: closed.</summary>"""
    _window(pen)
    pen.line([(40, 48), (60, 68)], RED)
    pen.line([(60, 48), (40, 68)], RED)


def _hourglass(pen: Pen, level: float = 0.5) -> None:
    """<summary>An hourglass with its sand part way through.</summary>
    <param name="level">How much of the sand is still in the top, 1 down to 0.</param>"""
    pen.line([(30, 16), (70, 16)])
    pen.line([(30, 84), (70, 84)])
    pen.line([(32, 20), (32, 30), (48, 50), (32, 70), (32, 80)])
    pen.line([(68, 20), (68, 30), (52, 50), (68, 70), (68, 80)])
    # top sand: a triangle whose flat top drops as the level falls
    if level > 0.02:
        top = 46 - 22 * level
        half = (46 - top) / 20 * 15 + 2
        pen.polygon([(50 - half, top), (50 + half, top), (50, 46)], fill=WARM, width=None)
    # bottom sand: a mound that grows
    if level < 0.98:
        height = 22 * (1 - level)
        pen.polygon([(36, 78), (64, 78), (50, 78 - height)], fill=WARM, width=None)
    if 0.02 < level < 0.98:
        pen.line([(50, 50), (50, 76)], WARM, 3)


def timer(pen: Pen) -> None:
    """<summary>An hourglass half run: a timer.</summary>"""
    _hourglass(pen, 0.5)


def _stopwatch(pen: Pen, angle: float = 0.0) -> None:
    """<summary>A stopwatch with its hand at an angle.</summary>
    <param name="angle">Degrees clockwise from straight up.</param>"""
    pen.circle(50, 56, 28)
    pen.line([(50, 20), (50, 28)])
    pen.line([(42, 14), (58, 14)])
    pen.line([(70, 30), (76, 24)], STEEL, 6)
    for step in range(0, 360, 90):
        rad = math.radians(step - 90)
        pen.line([(50 + 22 * math.cos(rad), 56 + 22 * math.sin(rad)),
                  (50 + 26 * math.cos(rad), 56 + 26 * math.sin(rad))], STEEL, 4)
    rad = math.radians(angle - 90)
    pen.line([(50, 56), (50 + 20 * math.cos(rad), 56 + 20 * math.sin(rad))], SIGNAL, 6)
    pen.dot(50, 56, 4, SIGNAL)


def stopwatch(pen: Pen) -> None:
    """<summary>A stopwatch at the start: a stopwatch.</summary>"""
    _stopwatch(pen, 0)


def counter(pen: Pen) -> None:
    """<summary>Four tally marks and the stroke through them: a count.</summary>"""
    for index in range(4):
        x = 26 + index * 14
        pen.line([(x, 26), (x, 74)])
    pen.line([(16, 70), (84, 30)], SIGNAL)


def toggle_on(pen: Pen) -> None:
    """<summary>A switch with its knob to the right, lit: on.</summary>"""
    pen.rounded(12, 30, 88, 70, 20, SIGNAL, None, fill=SIGNAL)
    pen.circle(68, 50, 14, INK, None, fill=INK)


def toggle_off(pen: Pen) -> None:
    """<summary>A switch with its knob to the left, unlit: off.</summary>"""
    pen.rounded(12, 30, 88, 70, 20, STEEL)
    pen.circle(32, 50, 14, STEEL, None, fill=STEEL)


def camera(pen: Pen) -> None:
    """<summary>A camera body with its lens hood to the right: the camera on.</summary>"""
    pen.rounded(14, 32, 62, 68, 5, INK, None, fill=INK)
    pen.polygon([(62, 44), (86, 32), (86, 68), (62, 56)], fill=SIGNAL, width=None)


def camera_off(pen: Pen) -> None:
    """<summary>The camera with a line struck through it: the camera off.</summary>"""
    camera(pen)
    pen.line([(18, 82), (82, 18)], RED)


def mic(pen: Pen) -> None:
    """<summary>A microphone capsule on its stand: the microphone on.</summary>"""
    pen.rounded(38, 12, 62, 54, 12, INK, None, fill=INK)
    pen.arc(50, 46, 22, 0, 180, SIGNAL)
    pen.line([(50, 68), (50, 82)])
    pen.line([(36, 82), (64, 82)])


def mic_off(pen: Pen) -> None:
    """<summary>The microphone with a line struck through it: the microphone off.</summary>"""
    mic(pen)
    pen.line([(18, 82), (82, 18)], RED)


def play(pen: Pen) -> None:
    """<summary>A triangle pointing right: play.</summary>"""
    pen.polygon([(32, 22), (80, 50), (32, 78)], fill=SIGNAL, width=None)


def pause(pen: Pen) -> None:
    """<summary>Two bars: pause.</summary>"""
    pen.rounded(28, 22, 42, 78, 3, INK, None, fill=INK)
    pen.rounded(58, 22, 72, 78, 3, INK, None, fill=INK)


def stop(pen: Pen) -> None:
    """<summary>A square: stop.</summary>"""
    pen.rounded(26, 26, 74, 74, 5, INK, None, fill=INK)


def record(pen: Pen) -> None:
    """<summary>A red dot in a ring: record.</summary>"""
    pen.circle(50, 50, 28, INK)
    pen.circle(50, 50, 16, RED, None, fill=RED)


def reset(pen: Pen) -> None:
    """<summary>An arrow going back round a circle: reset.</summary>"""
    pen.arc(50, 52, 26, -40, 250, INK)
    pen.line([(60, 22), (72, 34), (58, 40)], INK, 6)


def plus(pen: Pen) -> None:
    """<summary>A plus sign.</summary>"""
    pen.line([(50, 24), (50, 76)], SIGNAL, 10)
    pen.line([(24, 50), (76, 50)], SIGNAL, 10)


def minus(pen: Pen) -> None:
    """<summary>A minus sign.</summary>"""
    pen.line([(24, 50), (76, 50)], SIGNAL, 10)


def bell(pen: Pen) -> None:
    """<summary>A bell with its clapper: an alert or a timer's end.</summary>"""
    pen.polygon([(30, 62), (30, 44), (36, 28), (50, 20), (64, 28), (70, 44), (70, 62), (78, 70), (22, 70)],
                fill=WARM, width=None)
    pen.arc(50, 74, 8, 0, 180, INK)
    pen.dot(50, 14, 4, WARM)


def bright(pen: Pen) -> None:
    """<summary>A sun with rays: brightness up.</summary>"""
    pen.circle(50, 50, 14, WARM, None, fill=WARM)
    for step in range(0, 360, 45):
        rad = math.radians(step)
        pen.line([(50 + 22 * math.cos(rad), 50 + 22 * math.sin(rad)),
                  (50 + 32 * math.cos(rad), 50 + 32 * math.sin(rad))], WARM, 6)


def dim(pen: Pen) -> None:
    """<summary>A crescent moon: brightness down.</summary>"""
    pen.circle(50, 50, 28, STEEL, None, fill=STEEL)
    pen.circle(62, 42, 24, (0, 0, 0, 0), None, fill=(0, 0, 0, 0))


# Animated icons take the loop position t in [0, 1) and draw one frame.

def hourglass_running(pen: Pen, t: float) -> None:
    """<summary>The hourglass with its sand running through over the loop.</summary>
    <param name="t">Loop position.</param>"""
    _hourglass(pen, 1 - t)


def stopwatch_running(pen: Pen, t: float) -> None:
    """<summary>The stopwatch with its hand going round once a loop.</summary>
    <param name="t">Loop position.</param>"""
    _stopwatch(pen, 360 * t)


def ring_running(pen: Pen, t: float) -> None:
    """<summary>A ring emptying clockwise from the top over the loop.</summary>
    <param name="t">Loop position.</param>"""
    pen.circle(50, 50, 30, STEEL, 6)
    if t < 0.999:
        pen.arc(50, 50, 30, -90, -90 + 360 * (1 - t), SIGNAL, 10)


def counting(pen: Pen, t: float) -> None:
    """<summary>Tally marks appearing one by one, then the stroke through them.</summary>
    <param name="t">Loop position.</param>"""
    shown = int(t * 6)
    for index in range(min(4, shown)):
        x = 26 + index * 14
        pen.line([(x, 26), (x, 74)])
    if shown >= 5:
        pen.line([(16, 70), (84, 30)], SIGNAL)


# The desktop set: the standard things a Linux desktop offers, drawn as plain
# pictograms so they read the same on any distribution. None of them is any
# desktop's own artwork: a folder is a folder, a gear is a gear.

CLEAR = (0, 0, 0, 0)    # paints transparency, to cut one shape out of another


def terminal(pen: Pen) -> None:
    """<summary>A window with a prompt and a cursor: a terminal.</summary>"""
    pen.rounded(12, 20, 88, 80, 8)
    pen.line([(26, 40), (38, 50), (26, 60)], SIGNAL)
    pen.line([(46, 62), (66, 62)])


def files(pen: Pen) -> None:
    """<summary>A folder with its tab showing: the file manager.</summary>"""
    pen.rounded(12, 22, 46, 40, 4, SIGNAL, None, fill=SIGNAL)
    pen.rounded(12, 32, 88, 80, 6, INK, None, fill=INK)


def home(pen: Pen) -> None:
    """<summary>A house with its door: the home folder.</summary>"""
    pen.line([(12, 50), (50, 16), (88, 50)])
    pen.line([(24, 42), (24, 82), (76, 82), (76, 42)])
    pen.rounded(42, 58, 58, 82, 3, SIGNAL, None, fill=SIGNAL)


def browser(pen: Pen) -> None:
    """<summary>A globe with its meridian and latitudes: the web browser.</summary>
    <remarks>The meridian is an ellipse, which the Pen has no helper for, so it
    goes straight to the draw handle in pixels.</remarks>"""
    pen.circle(50, 50, 36)
    pen.draw.ellipse([*pen.p(35, 15), *pen.p(65, 85)], outline=SIGNAL, width=pen.w(6))
    pen.line([(16, 50), (84, 50)], SIGNAL, 6)
    pen.line([(22, 32), (78, 32)], STEEL, 5)
    pen.line([(22, 68), (78, 68)], STEEL, 5)


def text_editor(pen: Pen) -> None:
    """<summary>A page with a folded corner and lines of text: the text editor.</summary>"""
    pen.polygon([(22, 12), (62, 12), (78, 28), (78, 86), (22, 86)])
    pen.line([(62, 12), (62, 28), (78, 28)])
    pen.line([(32, 44), (68, 44)], SIGNAL, 6)
    pen.line([(32, 58), (68, 58)], STEEL, 6)
    pen.line([(32, 72), (54, 72)], STEEL, 6)


def settings(pen: Pen) -> None:
    """<summary>A cog with eight teeth: the system settings.</summary>"""
    for index in range(8):
        angle = math.radians(index * 45)
        pen.line([(50 + 26 * math.cos(angle), 50 + 26 * math.sin(angle)),
                  (50 + 36 * math.cos(angle), 50 + 36 * math.sin(angle))], INK, 13)
    pen.dot(50, 50, 28)
    pen.dot(50, 50, 12, SIGNAL)


def system_monitor(pen: Pen) -> None:
    """<summary>A screen with a trace across it: the system monitor.</summary>"""
    pen.rounded(12, 20, 88, 80, 8)
    pen.line([(22, 56), (36, 56), (44, 36), (54, 68), (62, 48), (78, 48)], SIGNAL, 6)


def calculator(pen: Pen) -> None:
    """<summary>A calculator with its display and keys.</summary>"""
    pen.rounded(24, 12, 76, 88, 8)
    pen.rounded(33, 21, 67, 37, 3, SIGNAL, None, fill=SIGNAL)
    for x in (36, 50, 64):
        for y in (52, 64, 76):
            pen.dot(x, y, 4.5)


def screenshot(pen: Pen) -> None:
    """<summary>Four viewfinder corners round a lens: take a screenshot.</summary>"""
    pen.line([(14, 32), (14, 14), (32, 14)])
    pen.line([(68, 14), (86, 14), (86, 32)])
    pen.line([(86, 68), (86, 86), (68, 86)])
    pen.line([(32, 86), (14, 86), (14, 68)])
    pen.circle(50, 50, 14, SIGNAL)
    pen.dot(50, 50, 5, SIGNAL)


def lock(pen: Pen) -> None:
    """<summary>A padlock with its keyhole: lock the screen.</summary>"""
    pen.arc(50, 42, 16, 180, 360)
    pen.line([(34, 42), (34, 50)])
    pen.line([(66, 42), (66, 50)])
    pen.rounded(24, 46, 76, 86, 8, INK, None, fill=INK)
    pen.dot(50, 62, 6, SIGNAL)
    pen.line([(50, 64), (50, 74)], SIGNAL, 6)


def log_out(pen: Pen) -> None:
    """<summary>An arrow leaving through an open doorway: log out.</summary>"""
    pen.line([(46, 16), (18, 16), (18, 84), (46, 84)])
    pen.line([(38, 50), (84, 50)], SIGNAL)
    pen.line([(70, 36), (84, 50), (70, 64)], SIGNAL)


def power_off(pen: Pen) -> None:
    """<summary>The power symbol, a broken ring with a bar through the gap: shut down.</summary>"""
    pen.arc(50, 54, 30, -60, 240)
    pen.line([(50, 14), (50, 50)], RED)


def restart(pen: Pen) -> None:
    """<summary>A ring with an arrowhead on its open end: restart.</summary>
    <remarks>The arrowhead is worked out from the arc's end at 240 degrees and
    points along the way the arc travels, up and to the right into the gap.</remarks>"""
    pen.arc(50, 52, 30, -60, 240)
    pen.polygon([(43, 21), (37, 35), (28, 20)], fill=INK, width=4)
    pen.dot(50, 52, 7, SIGNAL)


def suspend(pen: Pen) -> None:
    """<summary>A crescent moon with a Z beside it: suspend to sleep.</summary>"""
    pen.dot(44, 56, 28)
    pen.dot(60, 44, 24, CLEAR)
    pen.line([(64, 16), (80, 16), (64, 32), (80, 32)], SIGNAL, 5)


def workspace_left(pen: Pen) -> None:
    """<summary>A screen with a chevron pointing away to its left: the workspace to the left.</summary>"""
    pen.rounded(48, 26, 88, 74, 6)
    pen.line([(36, 34), (18, 50), (36, 66)], SIGNAL, 10)


def workspace_right(pen: Pen) -> None:
    """<summary>A screen with a chevron pointing away to its right: the workspace to the right.</summary>"""
    pen.rounded(12, 26, 52, 74, 6)
    pen.line([(64, 34), (82, 50), (64, 66)], SIGNAL, 10)


def show_desktop(pen: Pen) -> None:
    """<summary>A monitor showing only its wallpaper: show the desktop.</summary>"""
    pen.rounded(12, 18, 88, 68, 6)
    pen.polygon([(22, 60), (40, 40), (52, 52), (60, 46), (78, 60)], fill=SIGNAL, width=None)
    pen.line([(50, 70), (50, 80)])
    pen.line([(34, 82), (66, 82)])


def app_grid(pen: Pen) -> None:
    """<summary>Nine tiles in a square, the middle one lit: the application launcher.</summary>"""
    for row in range(3):
        for column in range(3):
            x = 16 + column * 24
            y = 16 + row * 24
            colour = SIGNAL if (row, column) == (1, 1) else INK
            pen.rounded(x, y, x + 18, y + 18, 5, colour, None, fill=colour)


def search(pen: Pen) -> None:
    """<summary>A magnifying glass with a glint: search.</summary>"""
    pen.circle(42, 42, 24)
    pen.line([(60, 60), (82, 82)], INK, 12)
    pen.arc(42, 42, 13, 200, 260, SIGNAL, 5)


def clipboard(pen: Pen) -> None:
    """<summary>A clipboard with its clip and a few lines: the clipboard.</summary>"""
    pen.rounded(22, 18, 78, 88, 8)
    pen.rounded(36, 12, 64, 26, 4, SIGNAL, None, fill=SIGNAL)
    pen.line([(34, 44), (66, 44)], STEEL, 6)
    pen.line([(34, 58), (66, 58)], STEEL, 6)
    pen.line([(34, 72), (56, 72)], STEEL, 6)


def mail(pen: Pen) -> None:
    """<summary>An envelope with its flap: mail.</summary>"""
    pen.rounded(12, 24, 88, 76, 6)
    pen.line([(18, 32), (50, 54), (82, 32)], SIGNAL)


def calendar(pen: Pen) -> None:
    """<summary>A calendar page with its rings and one day lit: the calendar.</summary>"""
    pen.rounded(14, 20, 86, 84, 8)
    pen.line([(14, 38), (86, 38)])
    pen.line([(32, 12), (32, 26)], SIGNAL)
    pen.line([(68, 12), (68, 26)], SIGNAL)
    for x in (30, 50, 70):
        for y in (52, 70):
            pen.dot(x, y, 5, SIGNAL if (x, y) == (70, 52) else STEEL)


def music(pen: Pen) -> None:
    """<summary>Two quavers joined by a beam: the music player.</summary>"""
    pen.line([(36, 70), (36, 24)])
    pen.line([(76, 62), (76, 16)])
    pen.line([(36, 24), (76, 16)], SIGNAL, 10)
    pen.dot(28, 72, 10)
    pen.dot(68, 64, 10)


def video(pen: Pen) -> None:
    """<summary>A screen with sprocket holes and a play mark: the video player.</summary>"""
    pen.rounded(12, 22, 88, 78, 6)
    for x in (24, 38, 62, 76):
        pen.dot(x, 32, 3, STEEL)
        pen.dot(x, 68, 3, STEEL)
    pen.polygon([(43, 38), (62, 50), (43, 62)], fill=SIGNAL, colour=SIGNAL, width=4)


def image_viewer(pen: Pen) -> None:
    """<summary>A framed picture of hills under a sun: the image viewer.</summary>"""
    pen.rounded(12, 20, 88, 80, 6)
    pen.polygon([(20, 72), (40, 46), (54, 62), (64, 52), (80, 72)], fill=SIGNAL, width=None)
    pen.dot(66, 36, 7, WARM)


def download(pen: Pen) -> None:
    """<summary>An arrow coming down into a tray: downloads.</summary>"""
    pen.line([(50, 12), (50, 58)], SIGNAL)
    pen.line([(34, 44), (50, 60), (66, 44)], SIGNAL)
    pen.line([(16, 62), (16, 82), (84, 82), (84, 62)])


def trash(pen: Pen) -> None:
    """<summary>A bin with its lid and handle: the rubbish bin.</summary>"""
    pen.line([(16, 26), (84, 26)])
    pen.line([(40, 24), (40, 16), (60, 16), (60, 24)])
    pen.polygon([(24, 36), (76, 36), (70, 86), (30, 86)])
    pen.line([(42, 48), (42, 74)], STEEL, 6)
    pen.line([(58, 48), (58, 74)], STEEL, 6)


def wifi(pen: Pen) -> None:
    """<summary>Three rising arcs over a dot: the wireless network.</summary>"""
    for radius, colour in ((18, INK), (36, INK), (54, STEEL)):
        pen.arc(50, 78, radius, 225, 315, colour)
    pen.dot(50, 78, 7, SIGNAL)


def bluetooth(pen: Pen) -> None:
    """<summary>The angular rune that stands for a short range radio link.</summary>
    <remarks>Drawn from the two runes it has always been described as, not from
    any published logo artwork. The rune is cut out of the disc rather than
    drawn over it, so it keeps its contrast when a style repaints the disc.</remarks>"""
    pen.circle(50, 50, 38, SIGNAL, None, fill=SIGNAL)
    pen.line([(34, 36), (64, 64), (50, 78), (50, 22), (64, 36), (34, 64)], CLEAR, 7)


def keyboard(pen: Pen) -> None:
    """<summary>A keyboard with two rows of keys and a space bar.</summary>"""
    pen.rounded(10, 28, 90, 74, 8)
    for y in (41, 53):
        for x in (23, 36, 50, 64, 77):
            pen.dot(x, y, 3.5, STEEL)
    pen.line([(32, 64), (68, 64)], SIGNAL, 6)


def printer(pen: Pen) -> None:
    """<summary>A printer with paper going in and a page coming out.</summary>
    <remarks>The page out is filled with transparency first, so it cuts the
    body's outline where it passes in front of it.</remarks>"""
    pen.line([(28, 36), (28, 14), (72, 14), (72, 36)], STEEL)
    pen.rounded(12, 36, 88, 70, 6)
    pen.rounded(28, 56, 72, 86, 3, INK, STROKE, fill=CLEAR)
    pen.line([(38, 70), (62, 70)], STEEL, 5)
    pen.dot(78, 46, 4, SIGNAL)


def software(pen: Pen) -> None:
    """<summary>A box seen from its corner: software and updates.</summary>"""
    pen.polygon([(50, 14), (84, 31), (84, 69), (50, 86), (16, 69), (16, 31)])
    pen.line([(16, 31), (50, 48), (84, 31)])
    pen.line([(50, 48), (50, 86)], SIGNAL)


# The game flavoured sets. Each one is meant to sit well beside a kind of
# game, not to belong to any: gothic war in the far future, cheerful planet
# hopping, a modern strategy war, alien crystal and bio tech, a horror corps
# of marines in the dark, and neon street tech. Every glyph is a plain,
# generic object drawn from scratch. No emblem, faction mark, logo, creature
# design or name from any game is used or imitated, and the labels stay
# generic words for the same reason. Keep it that way: it is what makes the
# themes ours to ship.


def _star(pen: Pen, cx: float, cy: float, r: float, colour=SIGNAL) -> None:
    """<summary>A filled five pointed star.</summary>
    <param name="cx">Centre across.</param>
    <param name="cy">Centre down.</param>
    <param name="r">Radius to the points.</param>
    <param name="colour">The fill.</param>"""
    points = []
    for index in range(10):
        radius = r if index % 2 == 0 else r * 0.42
        angle = math.radians(-90 + index * 36)
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    pen.polygon(points, fill=colour, width=None)


def _hexagon(pen: Pen, cx: float, cy: float, r: float, colour=INK, width: float | None = 5, fill=None) -> None:
    """<summary>A flat topped hexagon by centre and corner radius.</summary>
    <param name="cx">Centre across.</param>
    <param name="cy">Centre down.</param>
    <param name="r">Centre to corner.</param>
    <param name="colour">Outline colour.</param>
    <param name="width">Outline weight, or None.</param>
    <param name="fill">Fill colour, or None.</param>"""
    points = [(cx + r * math.cos(math.radians(60 * k)), cy + r * math.sin(math.radians(60 * k))) for k in range(6)]
    pen.polygon(points, fill=fill, colour=colour, width=width)


def _drop(pen: Pen, cx: float, cy: float, r: float, colour=SIGNAL) -> None:
    """<summary>A falling drop, round at the bottom and pointed at the top.</summary>
    <param name="cx">Centre of the round part.</param>
    <param name="cy">Centre of the round part.</param>
    <param name="r">Radius of the round part.</param>
    <param name="colour">The fill.</param>"""
    pen.dot(cx, cy, r, colour)
    pen.polygon([(cx - r * 0.92, cy - r * 0.4), (cx, cy - r * 2.3), (cx + r * 0.92, cy - r * 0.4)],
                fill=colour, width=None)


def _eye(pen: Pen, cy: float, colour=INK) -> None:
    """<summary>The outline of an eye, two lids meeting at 26 and 74 across.</summary>
    <param name="cy">Where the corners of the eye sit down the grid.</param>
    <param name="colour">The lids.</param>
    <remarks>Each lid is an arc of a circle of radius 30 whose centre sits 18
    units beyond the other lid, which puts both corners on the same line.</remarks>"""
    pen.arc(50, cy + 18, 30, 217, 323, colour, 6)
    pen.arc(50, cy - 18, 30, 37, 143, colour, 6)


# Shared strategy commands, drawn once and carried by the three war themes.

def attack(pen: Pen) -> None:
    """<summary>A reticle with its centre marked: attack.</summary>"""
    pen.circle(50, 50, 26)
    for points in (((50, 12), (50, 30)), ((50, 70), (50, 88)), ((12, 50), (30, 50)), ((70, 50), (88, 50))):
        pen.line(list(points))
    pen.dot(50, 50, 7, RED)


def move(pen: Pen) -> None:
    """<summary>A dotted trail turning into an arrow: move.</summary>"""
    pen.dot(18, 82, 4, STEEL)
    pen.dot(29, 71, 4, STEEL)
    pen.line([(40, 60), (78, 22)], SIGNAL)
    pen.line([(56, 22), (78, 22), (78, 44)], SIGNAL)


def halt(pen: Pen) -> None:
    """<summary>An octagon with a bar across it: stop where you are.</summary>"""
    points = [(50 + 36 * math.cos(math.radians(22.5 + 45 * k)), 50 + 36 * math.sin(math.radians(22.5 + 45 * k)))
              for k in range(8)]
    pen.polygon(points)
    pen.line([(34, 50), (66, 50)], RED, 10)


def guard(pen: Pen) -> None:
    """<summary>A shield with a smaller one lit inside it: guard.</summary>"""
    pen.polygon([(50, 12), (82, 24), (78, 58), (50, 88), (22, 58), (18, 24)])
    pen.polygon([(50, 28), (66, 34), (64, 54), (50, 70), (36, 54), (34, 34)], fill=SIGNAL, width=None)


def repair(pen: Pen) -> None:
    """<summary>A spanner: repair.</summary>
    <remarks>The jaw is a ring with a wedge cut out of it towards the top
    right, so the cut paints transparency after the ring is drawn.</remarks>"""
    pen.line([(24, 78), (52, 50)], INK, 12)
    pen.circle(62, 38, 15, INK, 10)
    pen.polygon([(62, 38), (70, 6), (96, 30)], fill=CLEAR, width=None)
    pen.dot(24, 78, 4, SIGNAL)


def sell(pen: Pen) -> None:
    """<summary>A coin with a diamond stamped in it: sell for credit.</summary>"""
    pen.circle(50, 50, 34)
    pen.circle(50, 50, 24, STEEL, 4)
    pen.polygon([(50, 34), (63, 50), (50, 66), (37, 50)], fill=SIGNAL, width=None)


def rally(pen: Pen) -> None:
    """<summary>A pennant on a pole: the rally point.</summary>"""
    pen.line([(30, 86), (30, 14)])
    pen.polygon([(30, 16), (78, 28), (30, 46)], fill=SIGNAL, colour=SIGNAL, width=4)
    pen.line([(18, 86), (44, 86)], STEEL)


def squad(pen: Pen) -> None:
    """<summary>Three figures, the middle one forward: select the squad.</summary>"""
    pen.dot(28, 40, 9, STEEL)
    pen.dot(72, 40, 9, STEEL)
    pen.arc(28, 76, 16, 180, 360, STEEL)
    pen.arc(72, 76, 16, 180, 360, STEEL)
    pen.dot(50, 34, 11)
    pen.pie(28, 54, 72, 98, 180, 360, INK)


def upgrade(pen: Pen) -> None:
    """<summary>Three stacked chevrons, the top one lit: upgrade or promote.</summary>"""
    pen.line([(24, 38), (50, 20), (76, 38)], SIGNAL)
    pen.line([(24, 58), (50, 40), (76, 58)])
    pen.line([(24, 78), (50, 60), (76, 78)])


def build(pen: Pen) -> None:
    """<summary>A hammer standing on its handle: build.</summary>"""
    pen.line([(46, 36), (46, 84)], STEEL, 10)
    pen.rounded(20, 16, 76, 36, 4, INK, None, fill=INK)
    pen.line([(40, 60), (52, 60)], SIGNAL, 6)


# Gothic war in the far future: relics, armour and fire.

def skull(pen: Pen) -> None:
    """<summary>A skull with lit eyes.</summary>"""
    pen.dot(50, 42, 30)
    pen.rounded(34, 60, 66, 84, 6, INK, None, fill=INK)
    pen.dot(38, 44, 9, CLEAR)
    pen.dot(62, 44, 9, CLEAR)
    pen.dot(38, 45, 3.5, RED)
    pen.dot(62, 45, 3.5, RED)
    pen.polygon([(50, 54), (45, 63), (55, 63)], fill=CLEAR, width=None)
    for x in (42, 50, 58):
        pen.line([(x, 72), (x, 86)], CLEAR, 3)


def helm(pen: Pen) -> None:
    """<summary>A heavy armoured helmet with slit lenses and a grille.</summary>"""
    pen.rounded(24, 14, 76, 70, 22, INK, None, fill=INK)
    pen.polygon([(24, 50), (76, 50), (68, 86), (32, 86)], fill=INK, width=None)
    pen.polygon([(29, 38), (46, 43), (44, 50), (29, 47)], fill=RED, width=None)
    pen.polygon([(71, 38), (54, 43), (56, 50), (71, 47)], fill=RED, width=None)
    for x in (43, 50, 57):
        pen.line([(x, 64), (x, 80)], CLEAR, 3)


def saw_blade(pen: Pen) -> None:
    """<summary>A sword whose blade is edged with saw teeth.</summary>"""
    for y in range(14, 62, 8):
        pen.polygon([(60, y), (67, y + 4), (60, y + 8)], fill=STEEL, width=None)
        pen.polygon([(40, y), (33, y + 4), (40, y + 8)], fill=STEEL, width=None)
    pen.rounded(40, 10, 60, 64, 4, INK, None, fill=INK)
    pen.line([(50, 16), (50, 58)], CLEAR, 3)
    pen.line([(26, 68), (74, 68)], INK, 10)
    pen.line([(50, 72), (50, 82)], STEEL, 9)
    pen.dot(50, 85, 5, SIGNAL)


def sidearm(pen: Pen) -> None:
    """<summary>A heavy pistol, side on.</summary>"""
    pen.rounded(14, 28, 80, 44, 4, INK, None, fill=INK)
    pen.polygon([(52, 44), (70, 44), (78, 80), (60, 80)], fill=INK, width=None)
    pen.line([(40, 44), (40, 56), (54, 56)], STEEL, 5)
    pen.dot(20, 36, 3, CLEAR)
    pen.line([(60, 30), (74, 30)], SIGNAL, 4)


def wax_seal(pen: Pen) -> None:
    """<summary>A scalloped wax seal with two ribbons hanging from it.</summary>"""
    pen.polygon([(36, 54), (26, 88), (36, 83), (45, 60)], fill=STEEL, width=None)
    pen.polygon([(64, 54), (74, 88), (64, 83), (55, 60)], fill=STEEL, width=None)
    for k in range(12):
        angle = math.radians(30 * k)
        pen.dot(50 + 25 * math.cos(angle), 40 + 25 * math.sin(angle), 8, RED)
    pen.dot(50, 40, 26, RED)
    pen.circle(50, 40, 15, INK, 4)
    pen.dot(50, 40, 5, INK)


def cathedral(pen: Pen) -> None:
    """<summary>A tall spire between two towers, with a pointed window lit.</summary>"""
    pen.polygon([(24, 38), (32, 52), (32, 86), (16, 86), (16, 52)], fill=STEEL, width=None)
    pen.polygon([(76, 38), (84, 52), (84, 86), (68, 86), (68, 52)], fill=STEEL, width=None)
    pen.polygon([(50, 10), (62, 36), (62, 86), (38, 86), (38, 36)], fill=INK, width=None)
    pen.polygon([(50, 46), (56, 54), (56, 72), (44, 72), (44, 54)], fill=SIGNAL, width=None)


def banner(pen: Pen) -> None:
    """<summary>A war banner hanging from its crossbar, a sword on it.</summary>"""
    pen.line([(20, 16), (80, 16)])
    pen.dot(18, 16, 5)
    pen.dot(82, 16, 5)
    pen.polygon([(28, 20), (72, 20), (72, 78), (61, 68), (50, 82), (39, 68), (28, 78)], fill=SIGNAL, width=None)
    pen.line([(50, 28), (50, 60)], INK, 5)
    pen.line([(42, 50), (58, 50)], INK, 5)


def psychic(pen: Pen) -> None:
    """<summary>An open eye with rays rising from it.</summary>"""
    _eye(pen, 58)
    pen.dot(50, 58, 8, SIGNAL)
    pen.line([(50, 36), (50, 16)], STEEL, 6)
    pen.line([(34, 40), (24, 26)], STEEL, 6)
    pen.line([(66, 40), (76, 26)], STEEL, 6)


def orbital_strike(pen: Pen) -> None:
    """<summary>A beam coming straight down onto a ring of impact.</summary>"""
    pen.ellipse(20, 64, 80, 82)
    pen.polygon([(44, 10), (56, 10), (60, 70), (40, 70)], fill=WARM, width=None)
    pen.line([(16, 54), (26, 60)], STEEL, 5)
    pen.line([(84, 54), (74, 60)], STEEL, 5)


def drop_pod(pen: Pen) -> None:
    """<summary>A pod falling nose first with fire trailing above it.</summary>"""
    pen.polygon([(28, 32), (32, 14), (40, 24), (50, 6), (60, 24), (68, 14), (72, 32)], fill=WARM, width=None)
    pen.polygon([(50, 88), (28, 62), (28, 30), (72, 30), (72, 62)], fill=INK, width=None)
    pen.line([(40, 36), (40, 62)], CLEAR, 3)
    pen.line([(60, 36), (60, 62)], CLEAR, 3)
    pen.dot(50, 50, 5, SIGNAL)


def purge(pen: Pen) -> None:
    """<summary>A flame, burning hot at its heart: purge with fire.</summary>"""
    pen.polygon([(50, 10), (66, 34), (74, 56), (68, 76), (50, 88), (32, 76), (26, 56), (34, 40), (42, 50), (44, 30)],
                fill=RED, width=None)
    pen.polygon([(50, 44), (60, 60), (58, 76), (50, 82), (42, 76), (40, 62)], fill=WARM, width=None)


def scroll(pen: Pen) -> None:
    """<summary>An unrolled scroll with lines of writing.</summary>"""
    pen.rounded(26, 22, 74, 78, 0, STEEL, None, fill=STEEL)
    pen.rounded(18, 14, 82, 26, 6, INK, None, fill=INK)
    pen.rounded(18, 74, 82, 86, 6, INK, None, fill=INK)
    for y, x1 in ((38, 66), (48, 66), (58, 66), (68, 54)):
        pen.line([(34, y), (x1, y)], CLEAR, 4)


# Planet hopping: suits, tools and little machines, friendly and rounded.

def planet(pen: Pen) -> None:
    """<summary>A ringed planet.</summary>
    <remarks>The back half of the ring goes down first, then the planet, then
    the front half, so the ring passes behind and in front.</remarks>"""
    pen.ellipse(10, 40, 90, 62, STEEL, 6, start=180, end=360)
    pen.dot(50, 50, 26)
    pen.dot(40, 40, 5, SIGNAL)
    pen.dot(60, 60, 4, SIGNAL)
    pen.ellipse(10, 40, 90, 62, STEEL, 6, start=0, end=180)


def oxygen(pen: Pen) -> None:
    """<summary>Bubbles rising, the big one shining: oxygen.</summary>"""
    pen.circle(42, 58, 26)
    pen.arc(42, 58, 16, 200, 260, SIGNAL, 5)
    pen.circle(72, 28, 12, SIGNAL, 6)
    pen.circle(30, 20, 7, STEEL, 5)


def tether(pen: Pen) -> None:
    """<summary>Two posts with a line slung between them: a tether.</summary>"""
    pen.arc(50, 20, 36, 34, 146, SIGNAL, 5)
    pen.line([(20, 86), (20, 44)])
    pen.line([(80, 86), (80, 44)])
    pen.dot(20, 40, 7, SIGNAL)
    pen.dot(80, 40, 7, SIGNAL)
    pen.line([(12, 86), (28, 86)])
    pen.line([(72, 86), (88, 86)])


def backpack(pen: Pen) -> None:
    """<summary>A suit backpack with its pocket and handle.</summary>"""
    pen.arc(50, 22, 10, 180, 360)
    pen.rounded(24, 20, 76, 86, 14, INK, None, fill=INK)
    pen.rounded(34, 54, 66, 78, 6, SIGNAL, None, fill=SIGNAL)
    pen.line([(36, 34), (64, 34)], STEEL, 5)


def terrain_tool(pen: Pen) -> None:
    """<summary>A hand held shaping tool with its emitter lit.</summary>"""
    pen.line([(42, 62), (34, 84)], INK, 11)
    pen.rounded(16, 42, 66, 64, 8, INK, None, fill=INK)
    pen.polygon([(66, 46), (84, 38), (84, 68), (66, 60)], fill=STEEL, width=None)
    pen.dot(86, 53, 4, SIGNAL)
    pen.line([(26, 50), (48, 50)], SIGNAL, 4)


def rover(pen: Pen) -> None:
    """<summary>A little open topped buggy.</summary>"""
    pen.line([(30, 42), (38, 24), (62, 24), (68, 42)], STEEL, 6)
    pen.rounded(14, 40, 86, 62, 8, INK, None, fill=INK)
    pen.dot(30, 68, 12, STEEL)
    pen.dot(70, 68, 12, STEEL)
    pen.dot(30, 68, 4, SIGNAL)
    pen.dot(70, 68, 4, SIGNAL)


def shuttle(pen: Pen) -> None:
    """<summary>A stubby rocket lifting off.</summary>"""
    pen.polygon([(36, 52), (22, 74), (36, 68)], fill=STEEL, width=None)
    pen.polygon([(64, 52), (78, 74), (64, 68)], fill=STEEL, width=None)
    pen.polygon([(50, 10), (64, 30), (64, 68), (36, 68), (36, 30)], fill=INK, width=None)
    pen.dot(50, 40, 7, SIGNAL)
    pen.polygon([(42, 70), (58, 70), (50, 88)], fill=WARM, width=None)


def habitat(pen: Pen) -> None:
    """<summary>A domed shelter on a platform, with its door and aerial.</summary>"""
    pen.line([(50, 32), (50, 16)])
    pen.dot(50, 14, 4, SIGNAL)
    pen.pie(18, 30, 82, 94, 180, 360, INK)
    pen.rounded(42, 44, 58, 62, 4, SIGNAL, None, fill=SIGNAL)
    pen.rounded(12, 62, 88, 74, 4, STEEL, None, fill=STEEL)
    pen.line([(24, 74), (20, 86)], STEEL, 6)
    pen.line([(76, 74), (80, 86)], STEEL, 6)


def research(pen: Pen) -> None:
    """<summary>A flask with something bubbling in it: research.</summary>"""
    pen.polygon([(32, 64), (68, 64), (76, 80), (24, 80)], fill=SIGNAL, width=None)
    pen.polygon([(44, 16), (44, 38), (22, 80), (26, 86), (74, 86), (78, 80), (56, 38), (56, 16)])
    pen.line([(38, 14), (62, 14)])
    pen.dot(46, 54, 3, STEEL)
    pen.dot(54, 47, 2.5, STEEL)


def resource(pen: Pen) -> None:
    """<summary>A faceted lump of ore.</summary>"""
    pen.polygon([(34, 20), (66, 16), (84, 42), (74, 76), (40, 84), (18, 58)], fill=SIGNAL, width=None)
    for end in ((34, 20), (66, 16), (84, 42), (40, 84), (18, 58)):
        pen.line([(46, 46), end], INK, 4)


def battery(pen: Pen) -> None:
    """<summary>A battery two thirds charged.</summary>"""
    pen.rounded(40, 12, 60, 24, 2, INK, None, fill=INK)
    pen.rounded(22, 22, 78, 86, 6)
    pen.rounded(32, 36, 68, 46, 2, STEEL, None, fill=STEEL)
    pen.rounded(32, 52, 68, 62, 2, SIGNAL, None, fill=SIGNAL)
    pen.rounded(32, 68, 68, 76, 2, SIGNAL, None, fill=SIGNAL)


def solar(pen: Pen) -> None:
    """<summary>A tilted solar panel on its stand.</summary>"""
    pen.line([(50, 62), (50, 82)])
    pen.line([(32, 84), (68, 84)])
    pen.polygon([(24, 22), (88, 22), (76, 62), (12, 62)], fill=SIGNAL, width=None)
    pen.line([(45, 22), (33, 62)], CLEAR, 3)
    pen.line([(67, 22), (55, 62)], CLEAR, 3)
    pen.line([(18, 42), (82, 42)], CLEAR, 3)


def wind(pen: Pen) -> None:
    """<summary>A three bladed wind turbine.</summary>"""
    pen.polygon([(47, 44), (53, 44), (57, 88), (43, 88)], fill=STEEL, width=None)
    for degrees in (-90, 30, 150):
        angle = math.radians(degrees)
        pen.line([(50, 40), (50 + 30 * math.cos(angle), 40 + 30 * math.sin(angle))], INK, 7)
    pen.dot(50, 40, 6, SIGNAL)


def beacon(pen: Pen) -> None:
    """<summary>A lamp on a post sending out signal rings.</summary>"""
    pen.line([(50, 86), (50, 42)])
    pen.line([(36, 86), (64, 86)])
    pen.dot(50, 34, 9, SIGNAL)
    for radius in (19, 31):
        pen.arc(50, 34, radius, 150, 210, STEEL, 5)
        pen.arc(50, 34, radius, -30, 30, STEEL, 5)


# A modern strategy war: base buildings, vehicles and support.

def harvester(pen: Pen) -> None:
    """<summary>A heavy truck with a load of crystal in its hopper.</summary>"""
    pen.polygon([(44, 38), (52, 24), (60, 32), (68, 22), (78, 38)], fill=SIGNAL, width=None)
    pen.rounded(36, 36, 86, 66, 6, INK, None, fill=INK)
    pen.rounded(14, 44, 38, 66, 4, STEEL, None, fill=STEEL)
    pen.rounded(18, 48, 30, 56, 2, CLEAR, None, fill=CLEAR)
    for x in (26, 50, 74):
        pen.dot(x, 72, 9, STEEL)
        pen.dot(x, 72, 3, CLEAR)


def refinery(pen: Pen) -> None:
    """<summary>A works with a chimney, a silo and its loading door.</summary>"""
    pen.rounded(22, 24, 32, 50, 1, INK, None, fill=INK)
    pen.rounded(14, 46, 62, 86, 3, INK, None, fill=INK)
    pen.rounded(66, 24, 86, 86, 8, STEEL, None, fill=STEEL)
    pen.rounded(28, 64, 48, 86, 2, SIGNAL, None, fill=SIGNAL)
    pen.line([(62, 56), (66, 56)], STEEL, 5)


def power_plant(pen: Pen) -> None:
    """<summary>A cooling tower with steam, and a bolt beside it: power.</summary>"""
    pen.polygon([(18, 86), (28, 52), (24, 26), (56, 26), (52, 52), (62, 86)], fill=INK, width=None)
    pen.dot(34, 16, 5, STEEL)
    pen.dot(46, 14, 6, STEEL)
    pen.polygon([(78, 26), (64, 56), (74, 56), (68, 84), (88, 48), (78, 48), (86, 26)], fill=WARM, width=None)


def radar(pen: Pen) -> None:
    """<summary>A dish on a mast, sending waves up to the right.</summary>"""
    pen.line([(40, 56), (40, 86)])
    pen.line([(26, 86), (54, 86)])
    pen.pie(10, 26, 70, 86, 135, 315, INK)
    pen.line([(40, 56), (64, 32)], STEEL, 5)
    pen.dot(64, 32, 5, SIGNAL)
    pen.arc(64, 32, 11, -80, -10, SIGNAL, 5)
    pen.arc(64, 32, 20, -80, -10, SIGNAL, 5)


def barracks(pen: Pen) -> None:
    """<summary>A hut with a star on its roof: train soldiers.</summary>"""
    pen.polygon([(12, 48), (50, 18), (88, 48)], fill=INK, width=None)
    pen.rounded(20, 46, 80, 86, 2, STEEL, None, fill=STEEL)
    pen.rounded(42, 62, 58, 86, 6, CLEAR, None, fill=CLEAR)
    _star(pen, 50, 37, 8)


def vehicle_bay(pen: Pen) -> None:
    """<summary>A saw toothed works with a big shutter door: build vehicles.</summary>"""
    pen.polygon([(12, 86), (12, 44), (30, 28), (30, 44), (48, 28), (48, 44), (66, 28), (66, 44), (88, 44), (88, 86)],
                fill=INK, width=None)
    pen.rounded(36, 56, 80, 86, 2, STEEL, None, fill=STEEL)
    for y in (64, 72, 80):
        pen.line([(40, y), (76, y)], CLEAR, 3)
    pen.dot(22, 56, 4, SIGNAL)


def tank(pen: Pen) -> None:
    """<summary>A tank side on: treads, hull, turret and gun.</summary>"""
    pen.rounded(14, 68, 86, 86, 9, STEEL, None, fill=STEEL)
    for x in (26, 38, 50, 62, 74):
        pen.dot(x, 77, 3, CLEAR)
    pen.polygon([(12, 58), (88, 58), (82, 70), (18, 70)], fill=INK, width=None)
    pen.rounded(32, 42, 64, 60, 6, INK, None, fill=INK)
    pen.line([(62, 50), (88, 44)], INK, 6)
    pen.dot(42, 50, 3, SIGNAL)


def helicopter(pen: Pen) -> None:
    """<summary>A helicopter side on with its rotor and skids: air support.</summary>"""
    pen.line([(14, 28), (74, 28)], STEEL, 5)
    pen.line([(44, 28), (44, 40)], INK, 5)
    pen.line([(60, 52), (86, 46)], INK, 6)
    pen.line([(86, 36), (86, 56)], STEEL, 5)
    pen.rounded(20, 38, 64, 66, 13, INK, None, fill=INK)
    pen.rounded(24, 44, 38, 56, 5, SIGNAL, None, fill=SIGNAL)
    pen.line([(32, 66), (30, 76)], STEEL, 4)
    pen.line([(52, 66), (54, 76)], STEEL, 4)
    pen.line([(20, 76), (62, 76)], STEEL, 5)


def airstrike(pen: Pen) -> None:
    """<summary>A jet from above, diving on a target: call an airstrike.</summary>"""
    pen.polygon([(50, 12), (56, 32), (84, 50), (84, 57), (56, 51), (54, 68), (64, 76), (64, 81), (50, 77),
                 (36, 81), (36, 76), (46, 68), (44, 51), (16, 57), (16, 50), (44, 32)], fill=INK, width=None)
    pen.line([(50, 20), (50, 44)], STEEL, 4)
    pen.dot(50, 86, 3, RED)


def missile(pen: Pen) -> None:
    """<summary>A missile rising from open silo doors.</summary>"""
    pen.polygon([(42, 58), (32, 74), (42, 70)], fill=STEEL, width=None)
    pen.polygon([(58, 58), (68, 74), (58, 70)], fill=STEEL, width=None)
    pen.rounded(42, 24, 58, 72, 6, INK, None, fill=INK)
    pen.polygon([(42, 28), (50, 10), (58, 28)], fill=RED, width=None)
    pen.polygon([(44, 72), (56, 72), (50, 84)], fill=WARM, width=None)
    pen.line([(12, 86), (36, 86)], STEEL, 7)
    pen.line([(64, 86), (88, 86)], STEEL, 7)


def infantry(pen: Pen) -> None:
    """<summary>A soldier's helmet with a star on it: infantry.</summary>"""
    pen.pie(20, 20, 80, 80, 180, 360, INK)
    pen.line([(16, 50), (84, 50)], INK, 8)
    pen.arc(50, 50, 20, 20, 160, STEEL, 5)
    _star(pen, 50, 37, 8)


def fortify(pen: Pen) -> None:
    """<summary>A wall of blocks: fortify.</summary>"""
    for x0, y0, x1, y1, colour in ((14, 30, 48, 46, INK), (52, 30, 86, 46, INK),
                                   (14, 50, 30, 66, INK), (34, 50, 66, 66, STEEL), (70, 50, 86, 66, INK),
                                   (14, 70, 48, 86, INK), (52, 70, 86, 86, INK)):
        pen.rounded(x0, y0, x1, y1, 3, colour, None, fill=colour)
    pen.dot(50, 20, 4, SIGNAL)


# Alien crystal and bio tech.

def crystal(pen: Pen) -> None:
    """<summary>A cluster of crystal shards, the middle one burning.</summary>"""
    pen.polygon([(22, 40), (34, 52), (38, 80), (24, 80), (16, 54)], fill=INK, width=None)
    pen.polygon([(80, 32), (86, 56), (76, 80), (62, 80), (66, 50)], fill=INK, width=None)
    pen.polygon([(50, 10), (62, 30), (58, 80), (42, 80), (38, 30)], fill=SIGNAL, width=None)
    pen.line([(14, 84), (86, 84)], STEEL, 5)


def growth(pen: Pen) -> None:
    """<summary>A tendril winding upwards with pods on it.</summary>"""
    pen.line([(50, 88), (44, 70), (56, 52), (44, 34), (52, 16)])
    pen.dot(64, 60, 7, SIGNAL)
    pen.dot(35, 44, 6, SIGNAL)
    pen.dot(62, 27, 5, SIGNAL)
    pen.line([(46, 76), (36, 78)], STEEL, 4)
    pen.line([(52, 42), (60, 40)], STEEL, 4)


def rift(pen: Pen) -> None:
    """<summary>Nested broken rings swirling round a bright core: a rift.</summary>"""
    pen.arc(50, 50, 34, 0, 270, INK, 7)
    pen.arc(50, 50, 24, 90, 360, STEEL, 7)
    pen.arc(50, 50, 14, 180, 450, SIGNAL, 7)
    pen.dot(50, 50, 5, SIGNAL)


def obelisk(pen: Pen) -> None:
    """<summary>A standing shard with a crystal floating over it.</summary>"""
    pen.polygon([(50, 30), (60, 40), (56, 86), (44, 86), (40, 40)], fill=INK, width=None)
    pen.polygon([(50, 8), (58, 18), (50, 26), (42, 18)], fill=SIGNAL, width=None)
    pen.line([(30, 20), (36, 24)], STEEL, 4)
    pen.line([(70, 20), (64, 24)], STEEL, 4)
    pen.line([(28, 86), (72, 86)], STEEL)


def mind(pen: Pen) -> None:
    """<summary>A brain sending out a wave: the mind that commands.</summary>"""
    pen.dot(38, 48, 20)
    pen.dot(62, 48, 20)
    pen.line([(50, 30), (50, 64)], CLEAR, 4)
    pen.arc(38, 48, 10, 200, 340, CLEAR, 4)
    pen.arc(62, 48, 10, 200, 340, CLEAR, 4)
    pen.arc(38, 56, 8, 20, 160, CLEAR, 4)
    pen.arc(62, 56, 8, 20, 160, CLEAR, 4)
    pen.arc(50, 56, 32, 40, 140, SIGNAL, 5)


def tentacle(pen: Pen) -> None:
    """<summary>A tentacle curling up, thick at the root and fine at the tip.</summary>"""
    steps = 18
    for index in range(steps):
        t = index / (steps - 1)
        x = 30 + 36 * t + 14 * math.sin(2 * math.pi * t)
        y = 86 - 62 * t
        pen.dot(x, y, 11 - 7.5 * t, INK)
    for t in (0.2, 0.42, 0.64):
        x = 30 + 36 * t + 14 * math.sin(2 * math.pi * t)
        pen.dot(x - 4, 86 - 62 * t, 2.5, SIGNAL)


def warp(pen: Pen) -> None:
    """<summary>Beams rising from a pad: warp in or out.</summary>"""
    pen.ellipse(20, 70, 80, 86)
    for x, top in ((34, 26), (50, 14), (66, 26)):
        pen.line([(x, 70), (x, top)], SIGNAL, 5)
    pen.dot(26, 18, 3, STEEL)
    pen.dot(74, 18, 3, STEEL)
    pen.dot(42, 10, 2.5, STEEL)


def mutate(pen: Pen) -> None:
    """<summary>A twisted double strand with rungs across: mutate.</summary>"""
    steps = 40
    strand_a, strand_b = [], []
    for index in range(steps + 1):
        y = 14 + 72 * index / steps
        phase = 2 * math.pi * (y - 14) / 48
        strand_a.append((50 + 18 * math.sin(phase), y))
        strand_b.append((50 - 18 * math.sin(phase), y))
    for index in range(2, steps, 4):
        pen.line([strand_a[index], strand_b[index]], STEEL, 4)
    pen.line(strand_a, INK, 6)
    pen.line(strand_b, SIGNAL, 6)


def shield_dome(pen: Pen) -> None:
    """<summary>A lattice dome over a small building: a shield dome.</summary>"""
    pen.rounded(40, 62, 60, 76, 4, SIGNAL, None, fill=SIGNAL)
    pen.arc(50, 76, 36, 180, 360, INK, 7)
    pen.arc(50, 76, 24, 180, 360, STEEL, 4)
    for degrees in (210, 240, 270, 300, 330):
        angle = math.radians(degrees)
        pen.line([(50 + 24 * math.cos(angle), 76 + 24 * math.sin(angle)),
                   (50 + 36 * math.cos(angle), 76 + 36 * math.sin(angle))], STEEL, 4)
    pen.line([(10, 78), (90, 78)])


def saucer(pen: Pen) -> None:
    """<summary>A saucer with its cupola, casting a beam down.</summary>"""
    pen.line([(40, 60), (26, 86)], SIGNAL, 4)
    pen.line([(60, 60), (74, 86)], SIGNAL, 4)
    pen.pie(34, 22, 66, 54, 180, 360, STEEL)
    pen.ellipse(12, 36, 88, 58, INK, None, fill=INK)
    for x, y in ((26, 47), (50, 50), (74, 47)):
        pen.dot(x, y, 3.5, SIGNAL)


def seed_pod(pen: Pen) -> None:
    """<summary>A pod split down its middle, glowing inside, with roots.</summary>"""
    pen.line([(36, 80), (24, 88)], STEEL, 5)
    pen.line([(64, 80), (76, 88)], STEEL, 5)
    pen.ellipse(26, 14, 74, 84, INK, None, fill=INK)
    pen.polygon([(50, 20), (57, 38), (50, 56), (43, 38)], fill=SIGNAL, width=None)
    pen.arc(40, 62, 10, 90, 200, CLEAR, 3)
    pen.arc(60, 62, 10, -20, 90, CLEAR, 3)


def glyph(pen: Pen) -> None:
    """<summary>An angular alien rune in a triangle.</summary>
    <remarks>It was a ring once, and a vertical stroke with arms inside a
    ring reads as a peace sign, so the frame is a triangle now.</remarks>"""
    pen.polygon([(50, 12), (86, 78), (14, 78)], colour=STEEL, width=5)
    pen.line([(50, 34), (50, 70)])
    pen.circle(50, 44, 7, SIGNAL, 5)
    pen.line([(36, 66), (50, 56), (64, 66)], SIGNAL)


# Marines in the dark: survival gear and something in the vents.

def tracker(pen: Pen) -> None:
    """<summary>A hand held motion scanner with two contacts on its screen.</summary>"""
    _tracker_face(pen)
    pen.dot(34, 52, 5, SIGNAL)
    pen.dot(58, 42, 4, SIGNAL)


def _tracker_face(pen: Pen) -> None:
    """<summary>The scanner's case, range rings and origin, shared with the animated one.</summary>"""
    pen.rounded(14, 18, 86, 86, 8)
    for radius in (14, 28, 42):
        pen.arc(50, 78, radius, 222, 318, STEEL, 3)
    for degrees in (222, 318):
        angle = math.radians(degrees)
        pen.line([(50, 78), (50 + 42 * math.cos(angle), 78 + 42 * math.sin(angle))], STEEL, 3)
    pen.dot(50, 78, 4)


def rifle(pen: Pen) -> None:
    """<summary>A rifle side on, with its magazine and sight.</summary>"""
    pen.polygon([(12, 40), (12, 62), (22, 62), (30, 52)], fill=INK, width=None)
    pen.rounded(14, 40, 72, 54, 3, INK, None, fill=INK)
    pen.line([(70, 47), (88, 47)], INK, 5)
    pen.polygon([(38, 54), (48, 54), (44, 72), (34, 72)], fill=INK, width=None)
    pen.polygon([(54, 54), (64, 54), (62, 78), (52, 78)], fill=STEEL, width=None)
    pen.rounded(34, 32, 56, 40, 2, STEEL, None, fill=STEEL)
    pen.rounded(58, 43, 66, 50, 1, SIGNAL, None, fill=SIGNAL)


def flamer(pen: Pen) -> None:
    """<summary>A flame gun with its fuel tank, firing.</summary>"""
    pen.polygon([(60, 44), (76, 34), (88, 48), (80, 52), (88, 62), (74, 64), (60, 58)], fill=WARM, width=None)
    pen.polygon([(60, 48), (72, 44), (76, 52), (70, 58), (60, 56)], fill=RED, width=None)
    pen.rounded(12, 44, 60, 58, 4, INK, None, fill=INK)
    pen.rounded(24, 58, 46, 76, 5, STEEL, None, fill=STEEL)
    pen.polygon([(48, 58), (56, 58), (54, 74), (46, 74)], fill=INK, width=None)


def sentry(pen: Pen) -> None:
    """<summary>A gun on a tripod with its sensor lit: a sentry.</summary>"""
    pen.line([(50, 56), (24, 86)], STEEL, 6)
    pen.line([(50, 56), (76, 86)], STEEL, 6)
    pen.line([(50, 56), (50, 86)], STEEL, 6)
    pen.rounded(28, 30, 70, 58, 4, INK, None, fill=INK)
    pen.line([(68, 42), (88, 42)], INK, 7)
    pen.dot(40, 44, 5, SIGNAL)


def welder(pen: Pen) -> None:
    """<summary>A cutting torch throwing sparks: weld a door shut.</summary>"""
    pen.line([(18, 84), (50, 52)], INK, 11)
    pen.line([(50, 52), (62, 40)], STEEL, 7)
    for end in ((78, 16), (86, 30), (84, 44), (72, 20)):
        pen.line([(68, 34), end], WARM, 4)
    pen.dot(67, 35, 6, WARM)
    pen.line([(20, 30), (44, 30)], SIGNAL, 5)


def flare(pen: Pen) -> None:
    """<summary>A lit flare stick burning at its top.</summary>"""
    for degrees in range(0, 360, 45):
        angle = math.radians(degrees)
        pen.line([(50 + 10 * math.cos(angle), 26 + 10 * math.sin(angle)),
                  (50 + 18 * math.cos(angle), 26 + 18 * math.sin(angle))], WARM, 4)
    pen.dot(50, 26, 8, WARM)
    pen.rounded(42, 38, 58, 86, 4, RED, None, fill=RED)
    pen.rounded(42, 36, 58, 44, 2, STEEL, None, fill=STEEL)


def marine_helmet(pen: Pen) -> None:
    """<summary>A combat helmet with a lamp on its side.</summary>"""
    pen.pie(20, 18, 80, 78, 180, 360, INK)
    pen.rounded(18, 44, 82, 56, 4, INK, None, fill=INK)
    pen.rounded(18, 52, 30, 74, 4, INK, None, fill=INK)
    pen.rounded(70, 52, 82, 74, 4, INK, None, fill=INK)
    pen.rounded(60, 28, 76, 40, 3, STEEL, None, fill=STEEL)
    pen.dot(68, 34, 4, SIGNAL)


def ammo(pen: Pen) -> None:
    """<summary>A curved magazine with rounds showing at the top.</summary>"""
    pen.rounded(40, 10, 58, 20, 3, SIGNAL, None, fill=SIGNAL)
    pen.polygon([(36, 18), (62, 18), (68, 86), (44, 86)], fill=INK, width=None)
    for y in (32, 44, 56, 68):
        pen.line([(46, y), (58, y)], CLEAR, 3)


def medkit(pen: Pen) -> None:
    """<summary>A first aid case with its handle and a plus on it.</summary>"""
    pen.line([(38, 30), (38, 20), (62, 20), (62, 30)])
    pen.rounded(14, 30, 86, 82, 8, INK, None, fill=INK)
    pen.line([(50, 42), (50, 70)], SIGNAL, 9)
    pen.line([(36, 56), (64, 56)], SIGNAL, 9)


def grenade(pen: Pen) -> None:
    """<summary>A hand grenade with its lever and pin ring.</summary>"""
    pen.circle(30, 24, 7, STEEL, 4)
    pen.line([(58, 30), (72, 26), (74, 48)], STEEL, 5)
    pen.rounded(40, 22, 60, 34, 3, INK, None, fill=INK)
    pen.ellipse(24, 32, 76, 88, INK, None, fill=INK)
    for y in (48, 62, 76):
        pen.line([(30, y), (70, y)], CLEAR, 3)
    pen.line([(50, 36), (50, 86)], CLEAR, 3)


def vitals(pen: Pen) -> None:
    """<summary>A monitor showing a racing pulse: vital signs.</summary>"""
    pen.rounded(12, 24, 88, 76, 6, STEEL, 5)
    pen.line([(18, 52), (32, 52), (38, 40), (45, 64), (53, 30), (60, 58), (64, 52), (82, 52)], SIGNAL, 6)


def claw_marks(pen: Pen) -> None:
    """<summary>Three torn slashes: something was here.</summary>"""
    for index in range(3):
        sx, sy = 20 + 16 * index, 14
        ex, ey = 44 + 16 * index, 86
        mx, my = (sx + ex) / 2, (sy + ey) / 2
        pen.polygon([(sx, sy), (mx + 5, my - 2), (ex, ey), (mx - 5, my + 2)], fill=INK, width=None)


def acid(pen: Pen) -> None:
    """<summary>Drops falling into a pool that is eating the floor.</summary>"""
    _drop(pen, 38, 34, 9)
    _drop(pen, 62, 52, 8)
    pen.ellipse(18, 72, 82, 88, INK, 5)
    pen.ellipse(30, 76, 70, 84, SIGNAL, None, fill=SIGNAL)


def hive(pen: Pen) -> None:
    """<summary>A comb of cells, the middle one full.</summary>"""
    _hexagon(pen, 50, 50, 13, SIGNAL, None, fill=SIGNAL)
    for k in range(6):
        angle = math.radians(30 + 60 * k)
        _hexagon(pen, 50 + 22.5 * math.cos(angle), 50 + 22.5 * math.sin(angle), 13, INK, 5)


def vent(pen: Pen) -> None:
    """<summary>A duct grille with one slat bent out of place.</summary>"""
    pen.rounded(14, 20, 86, 80, 6)
    for y in (34, 58, 70):
        pen.line([(26, y), (74, y)], STEEL, 6)
    pen.line([(26, 46), (46, 46)], STEEL, 6)
    pen.line([(54, 50), (74, 40)], SIGNAL, 6)


def tracker_ping(pen: Pen, t: float) -> None:
    """<summary>The scanner with its pulse sweeping out and contacts showing as it passes.</summary>
    <param name="t">Loop position.</param>"""
    _tracker_face(pen)
    reach = 4 + 40 * t
    pen.arc(50, 78, reach, 222, 318, SIGNAL, 5)
    for x, y, r in ((34, 52, 5), (58, 42, 4), (44, 66, 3.5)):
        if math.hypot(x - 50, y - 78) < reach:
            pen.dot(x, y, r, SIGNAL)


# Neon street tech.

def optics(pen: Pen) -> None:
    """<summary>A synthetic eye with a targeting ring and a trace running off it.</summary>"""
    _eye(pen, 50)
    pen.circle(50, 50, 11, SIGNAL, 4)
    pen.dot(50, 50, 4)
    pen.line([(78, 50), (86, 50), (86, 70)], STEEL, 4)
    pen.dot(86, 72, 4, STEEL)
    pen.line([(14, 30), (24, 30), (24, 38)], STEEL, 4)


def chip(pen: Pen) -> None:
    """<summary>A microchip with its pins and a lit core.</summary>"""
    for offset in (40, 50, 60):
        pen.line([(offset, 18), (offset, 30)], STEEL, 4)
        pen.line([(offset, 70), (offset, 82)], STEEL, 4)
        pen.line([(18, offset), (30, offset)], STEEL, 4)
        pen.line([(70, offset), (82, offset)], STEEL, 4)
    pen.rounded(28, 28, 72, 72, 5, INK, None, fill=INK)
    pen.rounded(40, 40, 60, 60, 2, SIGNAL, None, fill=SIGNAL)


def breach(pen: Pen) -> None:
    """<summary>A padlock sprung open with glitches across it: break in.</summary>"""
    pen.arc(42, 38, 16, 180, 360)
    pen.line([(26, 38), (26, 50)])
    pen.rounded(24, 48, 76, 86, 6, INK, None, fill=INK)
    for x, y in ((34, 58), (46, 58), (58, 58), (34, 70), (58, 70)):
        pen.rounded(x, y, x + 7, y + 7, 1, CLEAR, None, fill=CLEAR)
    pen.line([(14, 64), (40, 64)], SIGNAL, 4)
    pen.line([(60, 76), (86, 76)], SIGNAL, 4)


def jack_in(pen: Pen) -> None:
    """<summary>A plug on its lead, ready to go in: jack in.</summary>"""
    pen.line([(42, 36), (42, 16)], STEEL, 6)
    pen.line([(58, 36), (58, 16)], STEEL, 6)
    pen.rounded(32, 34, 68, 66, 6, INK, None, fill=INK)
    pen.line([(50, 66), (50, 76), (66, 86)], SIGNAL, 6)
    pen.dot(50, 50, 4, SIGNAL)


def blade(pen: Pen) -> None:
    """<summary>A long, gently curved single edged sword.</summary>"""
    points = []
    for index in range(21):
        t = index / 20
        bulge = 6 * math.sin(math.pi * t)
        points.append((36 + 48 * t - bulge * 0.7, 64 - 50 * t - bulge * 0.7))
    pen.line(points, INK, 6)
    pen.line([(16, 86), (32, 70)], STEEL, 9)
    pen.line([(28, 60), (42, 74)], SIGNAL, 6)


def ride(pen: Pen) -> None:
    """<summary>A low sports car side on.</summary>"""
    pen.polygon([(12, 64), (15, 52), (36, 48), (48, 36), (70, 36), (82, 48), (88, 54), (88, 66), (12, 66)],
                fill=INK, width=None)
    pen.polygon([(40, 48), (50, 40), (66, 40), (74, 48)], fill=CLEAR, width=None)
    pen.dot(30, 68, 10, STEEL)
    pen.dot(72, 68, 10, STEEL)
    pen.dot(30, 68, 4, CLEAR)
    pen.dot(72, 68, 4, CLEAR)
    pen.line([(82, 54), (88, 56)], SIGNAL, 4)
    pen.line([(12, 56), (16, 56)], RED, 4)


def bike(pen: Pen) -> None:
    """<summary>A motorbike side on.</summary>"""
    pen.circle(26, 66, 13, INK, 6)
    pen.circle(74, 66, 13, INK, 6)
    pen.line([(26, 66), (44, 46), (64, 46), (74, 66)], SIGNAL, 6)
    pen.line([(44, 46), (50, 66)], SIGNAL, 6)
    pen.line([(64, 46), (70, 30), (80, 30)], STEEL, 5)
    pen.line([(36, 40), (52, 40)], INK, 6)


def creds(pen: Pen) -> None:
    """<summary>Two notes of money, one on the other.</summary>"""
    pen.rounded(22, 22, 88, 60, 6, STEEL, 5)
    pen.rounded(12, 38, 78, 78, 6, INK, None, fill=INK)
    pen.circle(45, 58, 10, CLEAR, 4)
    pen.dot(22, 48, 3, SIGNAL)
    pen.dot(68, 68, 3, SIGNAL)


def flatline(pen: Pen) -> None:
    """<summary>A pulse that beats once and then runs flat.</summary>"""
    pen.line([(12, 56), (26, 56), (32, 40), (38, 70), (44, 56)], INK, 6)
    pen.line([(44, 56), (88, 56)], RED, 6)


def data(pen: Pen) -> None:
    """<summary>A stack of disks seen from the side: stored data.</summary>"""
    pen.line([(22, 23), (22, 77)])
    pen.line([(78, 23), (78, 77)])
    pen.ellipse(22, 41, 78, 59, STEEL, 5, start=0, end=180)
    pen.ellipse(22, 68, 78, 86, INK, STROKE, start=0, end=180)
    pen.ellipse(22, 14, 78, 32, SIGNAL, None, fill=SIGNAL)


def drone(pen: Pen) -> None:
    """<summary>A four rotor drone from above with its eye lit.</summary>"""
    for x, y in ((24, 24), (76, 24), (24, 76), (76, 76)):
        pen.line([(50, 50), (x, y)], INK, 6)
        pen.circle(x, y, 11, STEEL, 5)
    pen.rounded(38, 38, 62, 62, 6, INK, None, fill=INK)
    pen.dot(50, 50, 5, SIGNAL)


def signal(pen: Pen) -> None:
    """<summary>A lattice mast broadcasting: the network.</summary>"""
    pen.line([(50, 40), (34, 86)])
    pen.line([(50, 40), (66, 86)])
    pen.line([(42, 62), (58, 62)], STEEL, 5)
    pen.line([(38, 76), (62, 76)], STEEL, 5)
    pen.dot(50, 34, 6, SIGNAL)
    for radius in (14, 26):
        pen.arc(50, 34, radius, 150, 210, SIGNAL, 5)
        pen.arc(50, 34, radius, -30, 30, SIGNAL, 5)


CONTROLS = {
    "volume-up": volume_up, "volume-down": volume_down, "mute": mute,
    "speaker": speaker, "headphones": headphones, "cycle": cycle,
    "window": window, "window-minimise": window_minimise, "window-maximise": window_maximise,
    "window-close": window_close,
    "timer": timer, "stopwatch": stopwatch, "counter": counter,
    "toggle-on": toggle_on, "toggle-off": toggle_off,
    "camera": camera, "camera-off": camera_off, "mic": mic, "mic-off": mic_off,
    "play": play, "pause": pause, "stop": stop, "record": record, "reset": reset,
    "plus": plus, "minus": minus, "bell": bell, "bright": bright, "dim": dim,
}
CONTROL_LABELS = {
    "volume-up": "Volume Up", "volume-down": "Volume Down", "mute": "Mute",
    "speaker": "Speaker", "headphones": "Headphones", "cycle": "Next Output",
    "window": "Window", "window-minimise": "Minimise", "window-maximise": "Maximise",
    "window-close": "Close Window",
    "timer": "Timer", "stopwatch": "Stopwatch", "counter": "Counter",
    "toggle-on": "Toggle On", "toggle-off": "Toggle Off",
    "camera": "Camera", "camera-off": "Camera Off", "mic": "Microphone", "mic-off": "Microphone Off",
    "play": "Play", "pause": "Pause", "stop": "Stop", "record": "Record", "reset": "Reset",
    "plus": "Plus", "minus": "Minus", "bell": "Bell", "bright": "Bright", "dim": "Dim",
    "hourglass-running": "Hourglass Running", "stopwatch-running": "Stopwatch Running",
    "ring-running": "Ring Running", "counting": "Counting",
}
CONTROL_ANIMATED = {
    "hourglass-running": hourglass_running, "stopwatch-running": stopwatch_running,
    "ring-running": ring_running, "counting": counting,
}
# Animated icons: frames per loop and how fast they play.
GIF_FRAMES = 24
GIF_FPS = 12

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


DESKTOP = {
    "terminal": terminal, "files": files, "home": home, "browser": browser,
    "text-editor": text_editor, "settings": settings, "system-monitor": system_monitor,
    "calculator": calculator, "screenshot": screenshot, "lock": lock, "log-out": log_out,
    "power-off": power_off, "restart": restart, "suspend": suspend,
    "workspace-left": workspace_left, "workspace-right": workspace_right,
    "show-desktop": show_desktop, "app-grid": app_grid, "search": search,
    "clipboard": clipboard, "mail": mail, "calendar": calendar, "music": music,
    "video": video, "image-viewer": image_viewer, "download": download, "trash": trash,
    "wifi": wifi, "bluetooth": bluetooth, "keyboard": keyboard, "printer": printer,
    "software": software,
}
DESKTOP_LABELS = {
    "terminal": "Terminal", "files": "Files", "home": "Home", "browser": "Browser",
    "text-editor": "Text Editor", "settings": "Settings", "system-monitor": "System Monitor",
    "calculator": "Calculator", "screenshot": "Screenshot", "lock": "Lock Screen",
    "log-out": "Log Out", "power-off": "Power Off", "restart": "Restart", "suspend": "Suspend",
    "workspace-left": "Workspace Left", "workspace-right": "Workspace Right",
    "show-desktop": "Show Desktop", "app-grid": "Applications", "search": "Search",
    "clipboard": "Clipboard", "mail": "Mail", "calendar": "Calendar", "music": "Music",
    "video": "Video", "image-viewer": "Images", "download": "Downloads", "trash": "Rubbish Bin",
    "wifi": "Wi-Fi", "bluetooth": "Bluetooth", "keyboard": "Keyboard", "printer": "Printer",
    "software": "Software",
}


class Sweet:
    """<summary>
    A gradient style: repaints a finished glyph so each part of it is filled
    with a two colour sweep instead of a flat colour.
    </summary>
    <remarks>
    This is what the Sherbet and Lagoon themes are. They are meant to sit
    comfortably beside a desktop using a bright gradient icon theme, so they
    borrow that general idea, saturated colour running corner to corner, and
    nothing else. Every shape is still this file's own geometry; no other
    theme's artwork is traced, sampled or copied, and it must stay that way
    for the same reason the flat sets are drawn in code.

    The style works on the Pen's oversized image before it is shrunk. At that
    point nothing is antialiased, so every pixel is exactly one palette colour
    or clear, and each colour can be picked out by an exact match and
    replaced. Doing it after the shrink would leave the soft edges in the old
    colour as a fringe.

    The roles map like this. INK, the body of a glyph, takes the icon's main
    sweep. SIGNAL, the accent, takes a second sweep from the same family so a
    lit part still stands out. STEEL, the quiet detail, takes the main sweep
    washed towards white, a pastel of it; fading it with transparency instead
    looked muddy brown on the dark key. RED and WARM keep their meaning, stop and warning, as
    sweeps of their own. A pixel of any other colour is left as it was.

    Which sweep an icon gets comes from a checksum of its id, not its place
    in the set, so adding an icon never reshuffles the colours of the rest.
    </remarks>
    """

    def __init__(self, sweeps: list[tuple[tuple, tuple]], red: tuple[tuple, tuple],
                 warm: tuple[tuple, tuple], quiet: float = 0.5) -> None:
        """<summary>
        A style from its colour sweeps.
        </summary>
        <param name="sweeps">Pairs of RGB colours, top left to bottom right, that
        icons are shared out among. Three or more, so the accent differs from
        the body.</param>
        <param name="red">The sweep for anything drawn in RED.</param>
        <param name="warm">The sweep for anything drawn in WARM.</param>
        <param name="quiet">How far STEEL parts are washed towards white, 0 to 1.</param>
        """
        self.sweeps = sweeps
        self.red = red
        self.warm = warm
        self.quiet = quiet
        self.effects: tuple = ()
        self._cache: dict[tuple, Image.Image] = {}

    def gradient(self, sweep: tuple[tuple, tuple]) -> Image.Image:
        """<summary>
        A full size RGBA square running diagonally from one colour to the other.
        </summary>
        <param name="sweep">The start and end colours, as RGB tuples.</param>
        <returns>An opaque image at the Pen's drawing size, cached per sweep.</returns>
        <remarks>
        Built small and scaled up bilinearly, which gives the same smooth ramp
        as building it full size for a fraction of the work.
        </remarks>
        """
        if sweep not in self._cache:
            steps = 64
            ramp = Image.new("L", (steps, steps))
            ramp.putdata([round((x + y) * 255 / (2 * (steps - 1))) for y in range(steps) for x in range(steps)])
            ramp = ramp.resize((SIZE * SCALE, SIZE * SCALE), Image.Resampling.BILINEAR)
            self._cache[sweep] = ImageOps.colorize(ramp, sweep[0], sweep[1]).convert("RGBA")
        return self._cache[sweep]

    def __call__(self, image: Image.Image, name: str) -> Image.Image:
        """<summary>
        Repaint one glyph drawn in the flat palette with this style's sweeps.
        </summary>
        <param name="image">The Pen's oversized, not yet shrunk, drawing.</param>
        <param name="name">The icon's id, which picks its sweep.</param>
        <returns>A new image the same size; the one passed in is not changed.</returns>
        """
        out = image.copy()
        for colour, sweep in self.roles(name):
            mask = _exact(image, colour)
            if mask.getbbox() is not None:
                out.paste(self.gradient(sweep), (0, 0), mask)
        for effect in self.effects:
            out = effect(out)
        return out

    def roles(self, name: str) -> list[tuple[tuple, tuple[tuple, tuple]]]:
        """<summary>
        Which sweep each palette colour becomes, for one icon.
        </summary>
        <param name="name">The icon's id.</param>
        <returns>Pairs of a palette colour and the sweep that replaces it.</returns>
        """
        pick = zlib.crc32(name.encode("utf-8")) % len(self.sweeps)
        main = self.sweeps[pick]
        accent = self.sweeps[(pick + 2) % len(self.sweeps)]
        pastel = tuple(tuple(round(c + (255 - c) * self.quiet) for c in end) for end in main)
        return [(INK, main), (SIGNAL, accent), (STEEL, pastel), (RED, self.red), (WARM, self.warm)]


def _exact(image: Image.Image, colour: tuple) -> Image.Image:
    """<summary>
    A mask of the pixels that are exactly one opaque colour.
    </summary>
    <param name="image">An RGBA image with hard edges.</param>
    <param name="colour">An RGBA colour with full opacity.</param>
    <returns>An L image, 255 where the pixel matches and 0 everywhere else.</returns>
    """
    mask = None
    for band, value in zip(image.split(), colour):
        hit = band.point(lambda v, value=value: 255 if v == value else 0)
        mask = hit if mask is None else ImageChops.multiply(mask, hit)
    return mask


# Warm sweeps: raspberry, grape, peach, bubblegum, lime.
SHERBET = Sweet([
    ((255, 95, 160), (255, 160, 100)),
    ((178, 102, 255), (255, 102, 196)),
    ((255, 206, 87), (255, 111, 97)),
    ((255, 138, 216), (150, 120, 255)),
    ((170, 240, 110), (50, 205, 160)),
], red=((255, 90, 120), (230, 50, 70)), warm=((255, 226, 90), (255, 150, 60)))

# Cool sweeps: sky, mint, violet, teal, lilac.
LAGOON = Sweet([
    ((64, 224, 255), (84, 112, 255)),
    ((72, 240, 170), (40, 170, 255)),
    ((150, 120, 255), (60, 200, 255)),
    ((40, 215, 205), (50, 120, 220)),
    ((200, 150, 255), (90, 230, 230)),
], red=((255, 110, 150), (235, 60, 90)), warm=((255, 226, 90), (255, 160, 70)))


class Livery(Sweet):
    """<summary>
    A fixed colour scheme with a finish: every icon gets the same sweep for
    each palette colour, then the finishing effects are laid over it.
    </summary>
    <remarks>
    Where <see cref="Sweet"/> shares its sweeps out among icons for variety,
    a livery is one look held across a whole set, which is what a themed set
    wants: every key reads as part of the same kit. The effects are what tell
    the themed sets apart beyond colour, an engraved edge, a glow, scan lines
    or a split, and each runs on the full size image in the order given.
    </remarks>
    """

    def __init__(self, ink: tuple, signal: tuple, steel: tuple, red: tuple, warm: tuple,
                 effects: tuple = ()) -> None:
        """<summary>
        A livery from one sweep per palette colour and its finishing effects.
        </summary>
        <param name="ink">The sweep for the body of each glyph.</param>
        <param name="signal">The sweep for the accent.</param>
        <param name="steel">The sweep for the quiet detail.</param>
        <param name="red">The sweep for stop and danger.</param>
        <param name="warm">The sweep for fire and warning.</param>
        <param name="effects">Functions from a full size RGBA image to a new
        one, run in order after the colours are laid.</param>
        """
        super().__init__([ink], red, warm)
        self.fixed = [(INK, ink), (SIGNAL, signal), (STEEL, steel), (RED, red), (WARM, warm)]
        self.effects = effects

    def roles(self, name: str) -> list[tuple[tuple, tuple[tuple, tuple]]]:
        """<summary>
        The same colours for every icon.
        </summary>
        <param name="name">Unused: a livery does not vary by icon.</param>
        <returns>The fixed pairs of palette colour and sweep.</returns>
        """
        return self.fixed


def _units(units: float) -> float:
    """<summary>Grid units to pixels on the full size drawing.</summary>
    <param name="units">A length in grid units.</param>
    <returns>The same length in pixels before the shrink.</returns>"""
    return units * SIZE * SCALE / UNITS


def _solid(size: tuple[int, int], colour: tuple, alpha: Image.Image) -> Image.Image:
    """<summary>One colour everywhere, with the transparency given.</summary>
    <param name="size">The image size.</param>
    <param name="colour">An RGB colour.</param>
    <param name="alpha">An L image the same size, used as the alpha.</param>
    <returns>A new RGBA image.</returns>"""
    image = Image.new("RGBA", size, tuple(colour[:3]) + (0,))
    image.putalpha(alpha)
    return image


def outline(colour: tuple, units: float):
    """<summary>
    A finish that puts a solid edge round the whole glyph, like an engraving
    or a sticker.
    </summary>
    <param name="colour">The edge colour, RGB.</param>
    <param name="units">Roughly how thick the edge is, in grid units.</param>
    <returns>An effect function for <see cref="Livery"/>.</returns>
    <remarks>
    The glyph's shape is blurred and then cut hard at a low level, which grows
    it outwards by about twice the blur. Far quicker than a true dilation at
    this size, and the corners come out round, which suits every set. Small
    holes cut into a glyph fill up with the edge colour, which on a dark key
    reads as the same hole.
    </remarks>
    """
    def apply(image: Image.Image) -> Image.Image:
        """<summary>Lay the edge under the glyph.</summary>
        <param name="image">The coloured full size glyph.</param>
        <returns>A new image.</returns>"""
        grown = image.getchannel("A").filter(ImageFilter.GaussianBlur(_units(units) / 1.9))
        back = _solid(image.size, colour, grown.point(lambda a: 255 if a > 8 else 0))
        back.alpha_composite(image)
        return back
    return apply


def glow(colour: tuple, units: float, strength: float):
    """<summary>
    A finish that puts a soft halo of light behind the glyph.
    </summary>
    <param name="colour">The halo colour, RGB.</param>
    <param name="units">How far the halo spreads, in grid units.</param>
    <param name="strength">How bright it is; 1 leaves the blur as it is.</param>
    <returns>An effect function for <see cref="Livery"/>.</returns>
    <remarks>
    A GIF keeps only fully clear or fully solid pixels, so on an animated
    icon most of the halo is dropped when the frames are written. The stills
    keep all of it.
    </remarks>
    """
    def apply(image: Image.Image) -> Image.Image:
        """<summary>Lay the halo under the glyph.</summary>
        <param name="image">The coloured full size glyph.</param>
        <returns>A new image.</returns>"""
        haze = image.getchannel("A").filter(ImageFilter.GaussianBlur(_units(units)))
        back = _solid(image.size, colour, haze.point(lambda a: min(255, round(a * strength))))
        back.alpha_composite(image)
        return back
    return apply


def scanlines(units: float, strength: float):
    """<summary>
    A finish that dims every other band across the glyph, like an old
    phosphor screen.
    </summary>
    <param name="units">The height of one light and one dark band together,
    in grid units. About three keeps the bands visible on the deck.</param>
    <param name="strength">How much the dark bands lose, 0 to 1.</param>
    <returns>An effect function for <see cref="Livery"/>.</returns>
    """
    def apply(image: Image.Image) -> Image.Image:
        """<summary>Dim the dark bands.</summary>
        <param name="image">The coloured full size glyph.</param>
        <returns>A new image.</returns>"""
        period = _units(units)
        column = Image.new("L", (1, image.height))
        low = round(255 * (1 - strength))
        column.putdata([255 if (y % period) < period / 2 else low for y in range(image.height)])
        bands = column.resize(image.size, Image.Resampling.NEAREST)
        out = image.copy()
        out.putalpha(ImageChops.multiply(image.getchannel("A"), bands))
        return out
    return apply


def split(units: float, left: tuple, right: tuple, opacity: float):
    """<summary>
    A finish that shows two coloured ghosts of the glyph either side of it,
    like a signal breaking up.
    </summary>
    <param name="units">How far each ghost is shifted, in grid units.</param>
    <param name="left">The colour of the ghost shifted left, RGB.</param>
    <param name="right">The colour of the ghost shifted right, RGB.</param>
    <param name="opacity">How solid the ghosts are, 0 to 1.</param>
    <returns>An effect function for <see cref="Livery"/>.</returns>
    <remarks>
    The shift wraps round the image edge, which never shows because every
    glyph keeps well inside its margins.
    </remarks>
    """
    def apply(image: Image.Image) -> Image.Image:
        """<summary>Lay the two ghosts under the glyph.</summary>
        <param name="image">The coloured full size glyph.</param>
        <returns>A new image.</returns>"""
        shift = round(_units(units))
        alpha = image.getchannel("A").point(lambda a: round(a * opacity))
        out = _solid(image.size, left, ImageChops.offset(alpha, -shift, 0))
        out.alpha_composite(_solid(image.size, right, ImageChops.offset(alpha, shift, 0)))
        out.alpha_composite(image)
        return out
    return apply


def _flat(colour: tuple) -> tuple[tuple, tuple]:
    """<summary>A sweep that does not change: one flat colour.</summary>
    <param name="colour">An RGB colour.</param>
    <returns>The colour paired with itself.</returns>"""
    return (colour, colour)


# Gothic war: tarnished gold and bone, crimson accents, an engraved dark edge.
GRIMDARK = Livery(
    ink=((250, 222, 140), (176, 120, 44)), signal=((214, 48, 48), (122, 16, 24)),
    steel=((236, 226, 196), (176, 162, 124)), red=((255, 70, 50), (170, 20, 20)),
    warm=((255, 206, 90), (230, 110, 30)), effects=(outline((30, 18, 10), 2.4),))

# Planet hopping: suit white, safety orange and teal, with a soft navy sticker edge.
WAYFARER = Livery(
    ink=_flat((250, 250, 246)), signal=((255, 176, 70), (245, 120, 40)),
    steel=((120, 214, 214), (70, 160, 184)), red=_flat((244, 96, 88)),
    warm=((255, 224, 96), (255, 176, 60)), effects=(outline((34, 48, 74), 1.8),))

# Modern strategy war: sand and olive, crystal green, a hard dark edge.
FIELD_COMMAND = Livery(
    ink=((238, 228, 184), (196, 180, 124)), signal=((140, 240, 90), (40, 170, 70)),
    steel=((160, 170, 128), (112, 122, 90)), red=((240, 76, 60), (190, 40, 40)),
    warm=((255, 206, 70), (240, 150, 30)), effects=(outline((14, 20, 10), 1.8),))

# Alien tech: violet shards with a red hot heart, glowing.
RIFT = Livery(
    ink=((236, 140, 255), (140, 60, 230)), signal=((255, 96, 80), (255, 176, 60)),
    steel=((206, 176, 255), (150, 120, 220)), red=((255, 84, 116), (210, 40, 80)),
    warm=((255, 196, 96), (255, 124, 60)), effects=(glow((190, 80, 255), 4.5, 1.4),))

# Marines in the dark: green phosphor with a glow and scan lines.
DEEP_COLONY = Livery(
    ink=((186, 255, 176), (84, 214, 104)), signal=_flat((236, 255, 214)),
    steel=((90, 170, 100), (64, 132, 74)), red=((255, 136, 64), (236, 80, 44)),
    warm=((255, 206, 90), (240, 156, 44)), effects=(glow((60, 255, 100), 4, 1.1), scanlines(3.2, 0.45)))

# Neon street tech: hazard yellow, cyan and hot pink, the signal splitting.
NEON_CHROME = Livery(
    ink=((252, 240, 30), (255, 196, 0)), signal=_flat((0, 238, 255)),
    steel=((255, 80, 180), (214, 40, 160)), red=_flat((255, 44, 96)),
    warm=((255, 156, 44), (255, 96, 44)),
    effects=(split(1.8, (0, 230, 255), (255, 40, 130), 0.8), glow((255, 220, 0), 3, 0.5)))

COMMANDS = {
    "attack": attack, "move": move, "halt": halt, "guard": guard, "repair": repair,
    "sell": sell, "rally": rally, "squad": squad, "upgrade": upgrade, "build": build,
}
COMMAND_LABELS = {
    "attack": "Attack", "move": "Move", "halt": "Halt", "guard": "Guard", "repair": "Repair",
    "sell": "Sell", "rally": "Rally Point", "squad": "Squad", "upgrade": "Upgrade", "build": "Build",
}
GRIMDARK_ICONS = {
    "skull": skull, "helm": helm, "saw-blade": saw_blade, "sidearm": sidearm, "wax-seal": wax_seal,
    "cathedral": cathedral, "banner": banner, "psychic": psychic, "orbital-strike": orbital_strike,
    "drop-pod": drop_pod, "purge": purge, "scroll": scroll, **COMMANDS,
}
GRIMDARK_LABELS = {
    "skull": "Skull", "helm": "Helm", "saw-blade": "Saw Blade", "sidearm": "Sidearm",
    "wax-seal": "Wax Seal", "cathedral": "Cathedral", "banner": "Banner", "psychic": "Psychic",
    "orbital-strike": "Orbital Strike", "drop-pod": "Drop Pod", "purge": "Purge", "scroll": "Scroll",
    **COMMAND_LABELS,
}
WAYFARER_ICONS = {
    "planet": planet, "oxygen": oxygen, "tether": tether, "backpack": backpack,
    "terrain-tool": terrain_tool, "rover": rover, "shuttle": shuttle, "habitat": habitat,
    "research": research, "resource": resource, "battery": battery, "solar": solar,
    "wind": wind, "beacon": beacon,
}
WAYFARER_LABELS = {
    "planet": "Planet", "oxygen": "Oxygen", "tether": "Tether", "backpack": "Backpack",
    "terrain-tool": "Terrain Tool", "rover": "Rover", "shuttle": "Shuttle", "habitat": "Habitat",
    "research": "Research", "resource": "Resource", "battery": "Battery", "solar": "Solar",
    "wind": "Wind", "beacon": "Beacon",
}
FIELD_ICONS = {
    "harvester": harvester, "refinery": refinery, "power-plant": power_plant, "radar": radar,
    "barracks": barracks, "vehicle-bay": vehicle_bay, "tank": tank, "helicopter": helicopter,
    "airstrike": airstrike, "missile": missile, "infantry": infantry, "fortify": fortify, **COMMANDS,
}
FIELD_LABELS = {
    "harvester": "Harvester", "refinery": "Refinery", "power-plant": "Power Plant", "radar": "Radar",
    "barracks": "Barracks", "vehicle-bay": "Vehicle Bay", "tank": "Tank", "helicopter": "Air Support",
    "airstrike": "Airstrike", "missile": "Missile", "infantry": "Infantry", "fortify": "Fortify",
    **COMMAND_LABELS,
}
RIFT_ICONS = {
    "crystal": crystal, "growth": growth, "rift": rift, "obelisk": obelisk, "mind": mind,
    "tentacle": tentacle, "warp": warp, "mutate": mutate, "shield-dome": shield_dome,
    "saucer": saucer, "seed-pod": seed_pod, "glyph": glyph, **COMMANDS,
}
RIFT_LABELS = {
    "crystal": "Crystal", "growth": "Growth", "rift": "Rift", "obelisk": "Obelisk", "mind": "Mind",
    "tentacle": "Tentacle", "warp": "Warp", "mutate": "Mutate", "shield-dome": "Shield Dome",
    "saucer": "Beam", "seed-pod": "Seed Pod", "glyph": "Glyph", **COMMAND_LABELS,
}
COLONY_ICONS = {
    "tracker": tracker, "rifle": rifle, "flamer": flamer, "sentry": sentry, "welder": welder,
    "flare": flare, "helmet": marine_helmet, "ammo": ammo, "medkit": medkit, "grenade": grenade,
    "vitals": vitals, "claw-marks": claw_marks, "acid": acid, "hive": hive, "vent": vent,
}
COLONY_LABELS = {
    "tracker": "Tracker", "rifle": "Rifle", "flamer": "Flamer", "sentry": "Sentry",
    "welder": "Weld Door", "flare": "Flare", "helmet": "Helmet", "ammo": "Ammo", "medkit": "Medkit",
    "grenade": "Grenade", "vitals": "Vitals", "claw-marks": "Claw Marks", "acid": "Acid",
    "hive": "Hive", "vent": "Vent", "tracker-ping": "Tracker Sweep",
}
COLONY_ANIMATED = {"tracker-ping": tracker_ping}
NEON_ICONS = {
    "optics": optics, "chip": chip, "breach": breach, "jack-in": jack_in, "blade": blade,
    "ride": ride, "bike": bike, "creds": creds, "flatline": flatline, "data": data,
    "drone": drone, "signal": signal, "skull": skull, "sidearm": sidearm,
}
NEON_LABELS = {
    "optics": "Optics", "chip": "Chip", "breach": "Breach", "jack-in": "Jack In", "blade": "Blade",
    "ride": "Ride", "bike": "Bike", "creds": "Creds", "flatline": "Flatline", "data": "Data",
    "drone": "Drone", "signal": "Signal", "skull": "Skull", "sidearm": "Sidearm",
}

SWEET_ICONS = {**CONTROLS, **DESKTOP}
SWEET_LABELS = {**CONTROL_LABELS, **DESKTOP_LABELS}


# Amber Console: an old single colour electroluminescent terminal, amber on
# black, the kind a weapons console in an eighties film ran on. The look is
# the whole of the borrowing: one colour, a coarse visible pixel grid, one
# pixel lines, and the selected thing shown in inverse video, a solid amber
# block with the picture cut out of it in black. No screen layout, name or
# mark from any film is reproduced; the words on the console icons are
# ordinary words.

GRID = 48   # the pretend display's pixels across one key: seven letters fit inside the frame


class GridPen(Pen):
    """<summary>
    A Pen that draws straight onto the console's coarse pixel grid, hard
    edged, with one pixel lines.
    </summary>
    <remarks>
    Every glyph in the file can be handed one of these instead of a Pen and
    comes out as pixel art rather than as a shrunk drawing, because all the
    geometry is in grid units and only the Pen knows about pixels. Shrinking
    the smooth drawing instead was tried and turned thin details into mud.

    Line weights collapse to one pixel, or two for the few strokes drawn
    heavier than the house weight, which is what the old terminals drew.
    Nothing is antialiased, so every pixel is exactly one palette colour or
    clear, which <see cref="_lit_pixels"/> depends on.
    </remarks>
    """

    def __init__(self) -> None:
        """<summary>A clear GRID square canvas at one grid unit per GRID/UNITS pixels.</summary>"""
        self.px = GRID / UNITS
        self.image = Image.new("RGBA", (GRID, GRID), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)

    def w(self, units: float = STROKE) -> int:
        """<summary>
        A stroke width on the grid: one pixel, or two for a heavy stroke.
        </summary>
        <param name="units">The width the glyph asked for, in grid units.</param>
        <returns>1 or 2.</returns>
        """
        return 2 if units > STROKE else 1


# A five by seven pixel font for the console words, capitals and digits only.
PIXEL_FONT = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10011", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "0": ["01110", "10011", "10101", "10101", "10101", "11001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
}


def _pixels(pen: Pen, x: int, y: int, width: int = 1, height: int = 1, colour=INK) -> None:
    """<summary>Fill a block of grid pixels, whatever size the pen really is.</summary>
    <param name="pen">A GridPen, or an ordinary Pen, which gets the block scaled up.</param>
    <param name="x">Left pixel on the GRID square.</param>
    <param name="y">Top pixel on the GRID square.</param>
    <param name="width">Pixels across.</param>
    <param name="height">Pixels down.</param>
    <param name="colour">The fill.</param>
    <remarks>Lets the lettered glyphs draw on any Pen, so the tool's own
    folder and sheet modes still work on this set.</remarks>"""
    scale = pen.image.width / GRID
    pen.draw.rectangle([x * scale, y * scale, (x + width) * scale - 1, (y + height) * scale - 1], fill=colour)


def _word(pen: Pen, text: str, top: int, colour=INK) -> None:
    """<summary>Write a word in the pixel font, centred across the grid.</summary>
    <param name="pen">Where to draw.</param>
    <param name="text">Capitals and digits from PIXEL_FONT; seven characters fit.</param>
    <param name="top">The grid row the letters start on.</param>
    <param name="colour">The ink.</param>"""
    width = len(text) * 6 - 1
    left = (GRID - width) // 2
    for index, char in enumerate(text):
        for row, bits in enumerate(PIXEL_FONT[char]):
            for column, bit in enumerate(bits):
                if bit == "1":
                    _pixels(pen, left + index * 6 + column, top + row, colour=colour)


def _panel_word(pen: Pen, text: str) -> None:
    """<summary>A word between two rules, the way a console lists an option.</summary>
    <param name="pen">Where to draw.</param>
    <param name="text">The word, seven characters at most.</param>"""
    width = len(text) * 6 - 1
    left = max(3, (GRID - width) // 2 - 3)
    right = min(GRID - 3, (GRID - width) // 2 + width + 3)
    _pixels(pen, left, 13, right - left, 1, STEEL)
    _word(pen, text, 19)
    _pixels(pen, left, 31, right - left, 1, STEEL)


def armed(pen: Pen) -> None:
    """<summary>The word ARMED as a console option.</summary>"""
    _panel_word(pen, "ARMED")


def safe(pen: Pen) -> None:
    """<summary>The word SAFE as a console option.</summary>"""
    _panel_word(pen, "SAFE")


def auto(pen: Pen) -> None:
    """<summary>The word AUTO as a console option.</summary>"""
    _panel_word(pen, "AUTO")


def manual(pen: Pen) -> None:
    """<summary>The word MANUAL as a console option.</summary>"""
    _panel_word(pen, "MANUAL")


def searching(pen: Pen) -> None:
    """<summary>The word SEARCH as a console option.</summary>"""
    _panel_word(pen, "SEARCH")


def engaged(pen: Pen) -> None:
    """<summary>The word ENGAGED as a console option.</summary>"""
    _panel_word(pen, "ENGAGED")


def test_mode(pen: Pen) -> None:
    """<summary>The word TEST as a console option.</summary>"""
    _panel_word(pen, "TEST")


def online(pen: Pen) -> None:
    """<summary>The word ON LINE as a console option.</summary>"""
    _panel_word(pen, "ONLINE")


def rounds(pen: Pen) -> None:
    """<summary>A boxed counter under the word ROUNDS.</summary>"""
    _word(pen, "ROUNDS", 8)
    pen.rounded(24, 44, 76, 76, 0)
    _word(pen, "99", 25)


def gauge(pen: Pen) -> None:
    """<summary>Two tick marked vertical scales, each with its pointer outside the ticks.</summary>"""
    for x, side, level in ((36, -1, 60), (64, 1, 36)):
        pen.line([(x, 14), (x, 84)])
        for index, y in enumerate(range(14, 86, 7)):
            pen.line([(x, y), (x + side * (6 if index % 2 == 0 else 3), y)])
        tip = x + side * 9
        back = x + side * 19
        pen.polygon([(tip, level), (back, level - 6), (back, level + 6)], fill=INK, width=None)


def critical(pen: Pen) -> None:
    """<summary>A warning triangle with a heavy exclamation mark.</summary>"""
    pen.polygon([(50, 14), (86, 80), (14, 80)])
    pen.line([(50, 36), (50, 60)], INK, 12)
    pen.dot(50, 70, 3.5)


def critical_flash(pen: Pen, t: float) -> bool:
    """<summary>The warning triangle, flashing between its two faces.</summary>
    <param name="t">Loop position.</param>
    <returns>True for the half of the loop drawn lit, which
    <see cref="Console.draw"/> takes as a request for the inverse face.</returns>"""
    critical(pen)
    return t >= 0.5


class Console:
    """<summary>
    The Amber Console style: glyphs drawn on the coarse grid, then shown as
    an amber panel on black, either normal or lit in inverse video.
    </summary>
    <remarks>
    Unlike the other styles this one owns the whole drawing, pen and all,
    because its glyphs are drawn on a <see cref="GridPen"/> rather than
    repainted afterwards. <see cref="render"/> and <see cref="write_theme"/>
    check for it and hand the work over.

    The key is filled edge to edge with the panel, so the key's own
    background colour does not show: the black is part of the look.

    Each still comes in two faces. The normal one is amber pixels on black
    inside a one pixel frame. The lit one is the solid block with the glyph
    in black, which is what a toggle's active picture should be.
    </remarks>
    """

    def __init__(self, amber: tuple, black: tuple, glow: float = 0.55, gap: int = 70) -> None:
        """<summary>A console style from its two colours.</summary>
        <param name="amber">The lit colour, RGB.</param>
        <param name="black">The unlit panel, RGB.</param>
        <param name="glow">How bright the phosphor halo is, 0 to 1.</param>
        <param name="gap">How dark the thin gaps between pixel rows are, 0 to 255.</param>"""
        self.amber = amber
        self.black = black
        self.glow = glow
        self.gap = gap

    def draw(self, glyph, lit: bool = False, t: float | None = None) -> Image.Image:
        """<summary>
        Draw one glyph as a finished key face.
        </summary>
        <param name="glyph">A glyph function, taking a Pen, and the loop
        position too when <paramref name="t"/> is given.</param>
        <param name="lit">True for the inverse video face.</param>
        <param name="t">The loop position of an animated glyph, or None.</param>
        <returns>A SIZE square RGBA image, opaque.</returns>
        <remarks>An animated glyph may return True to ask for the lit face
        on that frame, which is how the flashing warning flashes.</remarks>
        """
        pen = GridPen()
        asked = glyph(pen) if t is None else glyph(pen, t)
        return self.face(_lit_pixels(pen.image), lit or bool(asked))

    def face(self, pixels: Image.Image, lit: bool) -> Image.Image:
        """<summary>
        Turn a grid of lit pixels into the panel.
        </summary>
        <param name="pixels">An L image GRID square, 255 where the glyph is lit.</param>
        <param name="lit">True for the inverse video face.</param>
        <returns>A SIZE square RGBA image, opaque.</returns>
        <remarks>
        The grid is scaled up with no smoothing so every pixel stays square,
        then a soft halo of the lit pixels goes underneath for the phosphor,
        and a thin dark line along the bottom of each pixel row shows the
        display's structure.
        </remarks>
        """
        panel = Image.new("L", (GRID, GRID), 0)
        sketch = ImageDraw.Draw(panel)
        if lit:
            sketch.rectangle([1, 1, GRID - 2, GRID - 2], fill=255)
            on = ImageChops.subtract(panel, pixels)
        else:
            sketch.rectangle([1, 1, GRID - 2, GRID - 2], outline=255)
            on = ImageChops.lighter(panel, pixels)
        big = on.resize((SIZE, SIZE), Image.Resampling.NEAREST)
        out = Image.new("RGBA", (SIZE, SIZE), tuple(self.black) + (255,))
        halo = big.filter(ImageFilter.GaussianBlur(SIZE / GRID * 0.8)).point(lambda v: round(v * self.glow))
        out.alpha_composite(_solid(out.size, self.amber, halo))
        out.paste(Image.new("RGBA", out.size, tuple(self.amber) + (255,)), (0, 0), big)
        step = SIZE / GRID
        column = Image.new("L", (1, SIZE))
        column.putdata([self.gap if (y % step) > step - 1.6 else 0 for y in range(SIZE)])
        out.alpha_composite(_solid(out.size, (0, 0, 0), column.resize((SIZE, SIZE), Image.Resampling.NEAREST)))
        return out


def _lit_pixels(image: Image.Image) -> Image.Image:
    """<summary>
    Which grid pixels light up, from a glyph drawn in the flat palette.
    </summary>
    <param name="image">A GridPen's RGBA drawing.</param>
    <returns>An L image, 255 for a lit pixel.</returns>
    <remarks>
    The console has one colour, so the parts of a glyph that told themselves
    apart by colour, a knob on a switch, a lamp on a helmet, would merge into
    one blob. Wherever two different colours touch, the pixel on the near
    side is left dark, which draws a one pixel gap between them the way the
    old screens separated shapes.
    </remarks>
    """
    source = image.load()
    lit = Image.new("L", image.size, 0)
    out = lit.load()
    width, height = image.size
    for y in range(height):
        for x in range(width):
            here = source[x, y]
            if here[3] < 128:
                continue
            dark = False
            for nx, ny in ((x + 1, y), (x, y + 1)):
                if nx < width and ny < height:
                    there = source[nx, ny]
                    if there[3] >= 128 and there[:3] != here[:3]:
                        dark = True
            out[x, y] = 0 if dark else 255
    return lit


AMBER = Console(amber=(255, 184, 28), black=(14, 9, 2))

CONSOLE = {
    "armed": armed, "safe": safe, "auto": auto, "manual": manual, "searching": searching,
    "engaged": engaged, "test-mode": test_mode, "online": online, "rounds": rounds,
    "gauge": gauge, "critical": critical, "sentry": sentry, "tracker": tracker, "ammo": ammo,
    "target": attack,
}
CONSOLE_LABELS = {
    "armed": "Armed", "safe": "Safe", "auto": "Auto", "manual": "Manual", "searching": "Searching",
    "engaged": "Engaged", "test-mode": "Test", "online": "Online", "rounds": "Rounds",
    "gauge": "Gauge", "critical": "Critical", "sentry": "Sentry", "tracker": "Tracker", "ammo": "Ammo",
    "target": "Target", "critical-flash": "Critical Flashing", "tracker-ping": "Tracker Sweep",
}
AMBER_ICONS = {**CONTROLS, **DESKTOP, **CONSOLE}
AMBER_LABELS = {**CONTROL_LABELS, **DESKTOP_LABELS, **CONSOLE_LABELS}
AMBER_ANIMATED = {**CONTROL_ANIMATED, "tracker-ping": tracker_ping, "critical-flash": critical_flash}
LIT_SUFFIX = "-lit"


# The sets, by the slug their theme folder carries. Each is the theme name,
# its still icons, its labels, its animated icons, the prefix the tool once
# put on its files when writing them into a user's images folder (which the
# daemon uses to recognise those old copies), and the style that repaints
# each glyph, or None to leave it in the flat palette.
SETS = {
    "space-game": (THEME_NAME, ICONS, LABELS, {}, "sc-", None),
    "controls": ("Controls", CONTROLS, CONTROL_LABELS, CONTROL_ANIMATED, "", None),
    "desktop": ("Desktop", DESKTOP, DESKTOP_LABELS, {}, "", None),
    "sherbet": ("Sherbet", SWEET_ICONS, SWEET_LABELS, CONTROL_ANIMATED, "", SHERBET),
    "lagoon": ("Lagoon", SWEET_ICONS, SWEET_LABELS, CONTROL_ANIMATED, "", LAGOON),
    "grimdark": ("Grimdark", GRIMDARK_ICONS, GRIMDARK_LABELS, {}, "", GRIMDARK),
    "wayfarer": ("Wayfarer", WAYFARER_ICONS, WAYFARER_LABELS, {}, "", WAYFARER),
    "field-command": ("Field Command", FIELD_ICONS, FIELD_LABELS, {}, "", FIELD_COMMAND),
    "rift": ("Rift", RIFT_ICONS, RIFT_LABELS, {}, "", RIFT),
    "deep-colony": ("Deep Colony", COLONY_ICONS, COLONY_LABELS, COLONY_ANIMATED, "", DEEP_COLONY),
    "neon-chrome": ("Neon Chrome", NEON_ICONS, NEON_LABELS, {}, "", NEON_CHROME),
    "amber-console": ("Amber Console", AMBER_ICONS, AMBER_LABELS, AMBER_ANIMATED, "", AMBER),
}


def write_theme(folder: Path, set_name: str = "space-game") -> int:
    """<summary>
    Write a set as a shipped theme: bare id.png files, id.gif for the animated
    ones, and a manifest.json.
    </summary>
    <param name="folder">Where to write. It is created if missing, and same
    named files in it are overwritten.</param>
    <param name="set_name">A key of <see cref="SETS"/>.</param>
    <returns>How many icons were written, not counting the manifest.</returns>
    <remarks>
    A set drawn in the <see cref="Console"/> style also gets an
    ``<id>-lit.png`` for every still, its inverse video face, listed in the
    manifest straight after the plain one so the picker shows them in pairs.

    This is the form deckplate/themes/ holds a theme in, so the picker can
    offer the set as a theme instead of it living among the user's own uploads.
    The file names carry no prefix here, unlike the user folder output, and the
    manifest is what supplies the human readable labels.

    Every icon in the set must have an entry in its labels or this raises
    part way through, leaving a folder of images with no manifest beside them.
    </remarks>
    """
    import json
    name, icons, labels, animated, prefix, style = SETS[set_name]
    folder.mkdir(parents=True, exist_ok=True)
    for icon in icons:
        render(icon, icons, style).save(folder / f"{icon}.png")
    for icon in animated:
        save_gif(render_frames(animated[icon], style=style, name=icon), folder / f"{icon}.gif")
    listed = [{"id": icon, "label": labels[icon]} for icon in list(icons) + list(animated)]
    if isinstance(style, Console):
        # Every still also gets its lit face, listed straight after it.
        for icon in icons:
            style.draw(icons[icon], lit=True).save(folder / f"{icon}{LIT_SUFFIX}.png")
        lit = {icon: {"id": f"{icon}{LIT_SUFFIX}", "label": f"{labels[icon]} Lit"} for icon in icons}
        listed = [entry for item in listed for entry in ([item, lit[item["id"]]] if item["id"] in lit else [item])]
    manifest = {"name": name, "file_prefix": prefix, "icons": listed}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return len(icons) + len(animated)


def render(name: str, icons: dict | None = None, style=None) -> Image.Image:
    """<summary>
    Draw one named icon on a fresh Pen and return the finished image.
    </summary>
    <param name="name">A key of the set given.</param>
    <param name="icons">The set to draw from, or None for the space game set.</param>
    <param name="style">A <see cref="Sweet"/> to repaint the glyph with, or
    None to keep the flat palette.</param>
    <returns>One icon as a SIZE square RGBA image, background transparent.</returns>
    <remarks>
    A new Pen every time, so two calls never share a canvas. Nothing is cached:
    rendering the whole set is a few seconds and the tool runs rarely.
    </remarks>
    
    <exception cref="KeyError">There is no icon by that name.</exception>"""
    if isinstance(style, Console):
        return style.draw((icons or ICONS)[name])
    pen = Pen()
    (icons or ICONS)[name](pen)
    return pen.result(style, name)


def render_frames(draw, count: int = GIF_FRAMES, style=None, name: str = "") -> list[Image.Image]:
    """<summary>
    Draw every frame of one loop of an animated icon.
    </summary>
    <param name="draw">The icon function, taking a Pen and the loop position.</param>
    <param name="count">Frames per loop.</param>
    <param name="style">A <see cref="Sweet"/> to repaint each frame with, or None.</param>
    <param name="name">The icon's id, which a style uses to pick its colours,
    so every frame of one icon gets the same ones.</param>
    <returns>The frames in order, each a SIZE square RGBA image.</returns>
    """
    if isinstance(style, Console):
        return [style.draw(draw, t=index / count) for index in range(count)]
    frames = []
    for index in range(count):
        pen = Pen()
        draw(pen, index / count)
        frames.append(pen.result(style, name))
    return frames


def save_gif(frames: list[Image.Image], path: Path, fps: int = GIF_FPS) -> None:
    """<summary>
    Write RGBA frames as a looping GIF with a transparent background.
    </summary>
    <param name="frames">The frames, all the same size.</param>
    <param name="path">Where to write.</param>
    <param name="fps">Frames a second.</param>
    <remarks>
    GIF has one bit of transparency and a palette per frame, so every frame
    is quantised against one palette built from all of them, and any pixel
    that was less than half opaque becomes the transparent index. The
    palette is built with a slot spared for that index. Disposal 2 clears
    each frame before the next, which is what a transparent animation
    needs or the frames pile up.
    </remarks>
    """
    sheet = Image.new("RGB", (frames[0].width * len(frames), frames[0].height), (0, 0, 0))
    for index, frame in enumerate(frames):
        sheet.paste(frame.convert("RGB"), (index * frame.width, 0))
    palette = sheet.quantize(colors=255)
    out = []
    for frame in frames:
        indexed = frame.convert("RGB").quantize(palette=palette)
        mask = frame.getchannel("A").point(lambda a: 255 if a < 128 else 0)
        indexed.paste(255, mask)
        out.append(indexed)
    out[0].save(path, save_all=True, append_images=out[1:], duration=int(1000 / fps), loop=0,
                disposal=2, transparency=255, optimize=False)



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
    parser.add_argument("--set", choices=sorted(SETS), default="space-game",
                        help="which set of icons (default: space-game)")
    parser.add_argument("--sheet", help="also write a contact sheet PNG to this path")
    args = parser.parse_args()
    theme_name, icons, _labels, animated, _prefix, style = SETS[args.set]
    if args.list:
        print("\n".join(list(icons) + [f"{name} (animated)" for name in animated]))
        return 0
    if args.theme:
        folder = Path(args.theme).expanduser()
        count = write_theme(folder, args.set)
        print(f"wrote theme '{theme_name}' ({count} icons and a manifest) into {folder}")
        return 0
    if not args.folder:
        parser.error("give a folder to write into, --theme DIR, or --list")
    folder = Path(args.folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    rendered = {}
    for name in icons:
        rendered[name] = render(name, icons, style)
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
