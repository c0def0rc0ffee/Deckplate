"""<summary>
The look a theme lends to the tiles the daemon draws itself: a palette and
a finish, so a clock or a gauge comes out in the same phosphor as the
theme's icons rather than in the plain white on navy.
</summary>
<remarks>
A theme's icons are pictures, drawn once by the icon tool and shipped. The
clock, the date, the weather, the machine gauges, the Pi-hole and the NAS
cannot be pictures: they are drawn fresh from a reading every time. This
module is what lets a theme reach them. A theme that wants to declares a
``look`` block in its manifest, naming its colours and the finish its icons
wear, and the tiles are then drawn with those colours and put through the
same finish, at three times the size and shrunk, exactly as the icons were.

The finish here mirrors the icon tool's tube: a halo under whatever is lit,
the corners falling away to the dark, and scan lines over everything. The
halo takes its colour from what is drawn, so a red alert glows red and the
green glows green, which is what lets one finish serve any palette.

Nothing here reads the config or the disk. A <see cref="Look"/> is built
from a manifest block by <see cref="parse"/> and handed in; the themes
module is the one that finds the block.
</remarks>
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from PIL import Image, ImageChops, ImageEnhance, ImageFilter

from . import images

# The size the icon tool draws its finished faces at. The finish's lengths
# (the halo's spread, the scan line pitch) are given in the manifest at this
# size, so a tile drawn at any other size scales them to match.
FULL = 285
# Themed tiles are drawn this many times larger than asked and shrunk, so a
# deck key at 95 px is finished at exactly the icon tool's size and the scan
# lines come out as the same texture the icons carry.
OVERSAMPLE = 3

Colour = tuple[int, int, int]


@dataclass(frozen=True)
class Palette:
    """<summary>
    The colours a drawn tile uses, by role rather than by value.
    </summary>
    <remarks>
    The roles are the ones the tile functions already drew with as module
    constants: the ground, the main text, the quieter detail, the bar fill,
    the alarm, the bar track, and the five weather glyph colours. The plain
    palette holds the original values; a themed one replaces them.
    </remarks>
    """

    background: Colour
    foreground: Colour
    muted: Colour
    accent: Colour
    alert: Colour
    track: Colour
    sun: Colour
    cloud: Colour
    rain: Colour
    snow: Colour
    bolt: Colour

    def on(self, background: Colour) -> "Palette":
        """<summary>The same palette over another ground.</summary>
        <param name="background">The RGB to draw on.</param>
        <returns>A copy with only the background changed.</returns>"""
        return self if background == self.background else replace(self, background=background)


PLAIN = Palette(background=images.BACKGROUND, foreground=images.FOREGROUND, muted=(150, 165, 180),
                accent=images.ACCENT, alert=(232, 96, 96), track=(44, 54, 70),
                sun=(255, 200, 60), cloud=(215, 222, 230), rain=(110, 170, 240),
                snow=(240, 245, 255), bolt=(255, 220, 80))


@dataclass(frozen=True)
class Look:
    """<summary>
    One theme's look for the drawn tiles: its palette and its finish.
    </summary>
    <remarks>
    ``glow`` is how bright the halo under the lit parts is, 0 for none and 1
    for the blur as it comes. ``pitch`` is the pixels from one scan line to
    the next at <see cref="FULL"/> size, ``lines`` how dark the line is out
    of 255, and ``fall`` how far the corners darken, 0 to 255. Zero for any
    of the three turns that part of the finish off, so a theme can have the
    palette without the tube.
    </remarks>
    """

    slug: str
    name: str
    palette: Palette
    glow: float = 0.5
    pitch: int = 4
    lines: int = 96
    fall: int = 110

    @property
    def background(self) -> str:
        """<summary>The look's ground as the hex text the config uses for a colour.</summary>
        <returns>``#rrggbb``.</returns>"""
        return "#%02x%02x%02x" % self.palette.background


def _rgb(value, where: str) -> Colour:
    """<summary>A colour from a manifest: a ``#rrggbb`` string or a three number list.</summary>
    <param name="value">The manifest value.</param>
    <param name="where">The field name, for the error.</param>
    <returns>An RGB tuple.</returns>
    <exception cref="ValueError">The value is neither form.</exception>"""
    if isinstance(value, str):
        text = value.strip()
        if len(text) == 7 and text[0] == "#":
            try:
                return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
            except ValueError:
                pass
        raise ValueError(f"{where}: not a colour")
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(isinstance(v, int) and 0 <= v <= 255 for v in value):
        return (value[0], value[1], value[2])
    raise ValueError(f"{where}: not a colour")


def _dim(colour: Colour, level: int) -> Colour:
    """<summary>A colour at a lower brightness, as a phosphor lit less hard.</summary>
    <param name="colour">The full colour.</param>
    <param name="level">The brightness out of 255.</param>
    <returns>The scaled RGB.</returns>"""
    return tuple(round(c * level / 255) for c in colour)  # type: ignore[return-value]


def parse(slug: str, name: str, block) -> Look | None:
    """<summary>
    A look from a theme manifest's ``look`` block, or None when it has none.
    </summary>
    <param name="slug">The theme's folder name.</param>
    <param name="name">The theme's display name.</param>
    <param name="block">The manifest's ``look`` value, usually a dict or absent.</param>
    <returns>A Look, or None for a theme with no look or an unreadable block.</returns>
    <remarks>
    Only two colours are required, ``ink`` for what is lit and ``black`` for
    the ground, which is all a monochrome screen has. The other roles are
    derived from them as dimmer phosphor unless the block names them: the
    quieter detail at just under half brightness, the bar fill a little under
    full, the track very dim, and the alarm the ink itself unless an
    ``alert`` colour is given. The weather glyphs are all ink or muted, so a
    sun and a cloud stay apart without a second colour.

    An unreadable block answers None rather than raising, in the spirit of
    the themes module: a bad look must not take the theme's icons with it.
    </remarks>
    """
    if not isinstance(block, dict):
        return None
    try:
        ink = _rgb(block["ink"], "ink")
        black = _rgb(block["black"], "black")
        muted = _rgb(block["muted"], "muted") if "muted" in block else _dim(ink, 118)
        accent = _rgb(block["accent"], "accent") if "accent" in block else _dim(ink, 196)
        alert = _rgb(block["alert"], "alert") if "alert" in block else ink
        track = _rgb(block["track"], "track") if "track" in block else _dim(ink, 36)
        glow = float(block.get("glow", 0.5))
        pitch = int(block.get("pitch", 4))
        lines = int(block.get("lines", 96))
        fall = int(block.get("fall", 110))
    except (KeyError, ValueError, TypeError):
        return None
    if not (0 <= glow <= 1 and 0 <= pitch <= 64 and 0 <= lines <= 255 and 0 <= fall <= 255):
        return None
    palette = Palette(background=black, foreground=ink, muted=muted, accent=accent, alert=alert, track=track,
                      sun=ink, cloud=muted, rain=ink, snow=ink, bolt=ink)
    return Look(slug=slug, name=name, palette=palette, glow=glow, pitch=pitch, lines=lines, fall=fall)


def finish(tile: Image.Image, look: Look, background: Colour | None = None) -> Image.Image:
    """<summary>
    Put a drawn tile through the look's finish: halo, corner fall, scan lines.
    </summary>
    <param name="tile">The tile as drawn, RGB, any square size.</param>
    <param name="look">The look whose finish to apply.</param>
    <param name="background">The ground the tile was drawn on, when it is
    not the look's own; the halo is of whatever stands out from it.</param>
    <returns>A new RGB image the same size.</returns>
    <remarks>
    The lengths scale with the tile so the finish reads the same at every
    size: the halo spreads three hundredths of the tile, as the icon tool's
    does, and the scan line pitch is the manifest's figure scaled from
    <see cref="FULL"/>, never under two pixels or the lines would be every
    row and the whole tile simply darker.

    The halo is the tile minus its ground, blurred and dimmed, added back on,
    so nothing glows where nothing is drawn and the glow takes the colour of
    what it is under. The corners and the scan lines are laid over the tile
    and halo both, as they are on the icons.
    </remarks>
    """
    size = tile.height
    tile = tile.convert("RGB")
    ground = images.solid(background or look.palette.background, size)
    out = tile
    if look.glow > 0:
        lit = ImageChops.subtract(tile, ground)
        halo = lit.filter(ImageFilter.GaussianBlur(size / 100 * 3.0))
        out = ImageChops.add(out, ImageEnhance.Brightness(halo).enhance(look.glow))
    if look.fall > 0:
        corner = Image.radial_gradient("L").resize((size, size), Image.Resampling.BILINEAR)
        corner = corner.point(lambda v: round(max(0, v - 90) * look.fall / 165))
        out = Image.composite(ground, out, corner)
    if look.lines > 0 and look.pitch > 0:
        pitch = max(2, round(look.pitch * size / FULL))
        column = Image.new("L", (1, size))
        column.putdata([look.lines if (y % pitch) == pitch - 1 else 0 for y in range(size)])
        mask = column.resize((size, size), Image.Resampling.NEAREST)
        out = Image.composite(images.solid((0, 0, 0), size), out, mask)
    return out


def render(draw: Callable[[int, Palette], Image.Image], size: int, background: Colour | None,
           look: Look | None) -> Image.Image:
    """<summary>
    Draw a tile plain, or draw it large in the look's palette and finish it.
    </summary>
    <param name="draw">The drawing, taking the size to draw at and the
    palette to draw with, giving an RGB tile of that size.</param>
    <param name="size">The size wanted.</param>
    <param name="background">The ground to draw on, or None for the
    palette's own: the plain navy, or the look's ground.</param>
    <param name="look">The look, or None for the plain drawing.</param>
    <returns>An RGB tile of ``size`` square.</returns>
    <remarks>
    This is the one place the two paths meet, so every tile function is the
    same short shape: its drawing as an inner function, and this call. With
    no look the drawing runs at the size asked and nothing else happens,
    which keeps the plain path byte for byte what it was. With a look it
    runs at <see cref="OVERSAMPLE"/> times the size, is finished there and
    shrunk with Lanczos, the way the icon tool makes its faces.
    </remarks>
    """
    if look is None:
        return draw(size, PLAIN.on(background or PLAIN.background))
    ground = background or look.palette.background
    big = draw(size * OVERSAMPLE, look.palette.on(ground))
    return finish(big, look, ground).resize((size, size), Image.Resampling.LANCZOS)
