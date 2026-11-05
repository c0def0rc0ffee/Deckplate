"""<summary>
A theme's look for the drawn tiles: the manifest block parsed, the palette
derived, and the finish seen in the pixels.
</summary>
<remarks>
As with the tile tests, the finish is checked by reading what comes out: a
halo is a pixel beside the ink that is brighter than the ground, scan lines
are rows that are darker than their neighbours, and the corner fall is a
corner darker than the middle. Coarse on purpose, so a nudge to a strength
does not break them.
</remarks>
"""

from datetime import datetime

from PIL import Image, ImageDraw

from deckplate import images, looks, themes, tiles

BLOCK = {"ink": [96, 255, 128], "black": [5, 16, 9], "glow": 0.5, "pitch": 4, "lines": 96, "fall": 110}


def test_parse_derives_the_quieter_roles_from_the_ink():
    """<summary>Two colours are enough: muted, accent and track are dimmer ink, alert the ink itself.</summary>"""
    look = looks.parse("tube", "Tube", BLOCK)
    assert look is not None and look.slug == "tube" and look.name == "Tube"
    p = look.palette
    assert p.foreground == (96, 255, 128) and p.background == (5, 16, 9)
    assert max(p.muted) < max(p.accent) < max(p.foreground)
    assert max(p.track) < max(p.muted)
    assert p.alert == p.foreground
    assert p.sun == p.foreground and p.cloud == p.muted
    assert look.background == "#051009"


def test_parse_takes_named_roles_and_hex_colours():
    """<summary>A block may name the alert and the others, as hex or as a list.</summary>"""
    look = looks.parse("t", "T", {"ink": "#60ff80", "black": "#051009", "alert": [255, 110, 80], "muted": "#2c763b"})
    assert look is not None
    assert look.palette.alert == (255, 110, 80) and look.palette.muted == (44, 118, 59)
    assert look.palette.foreground == (96, 255, 128)


def test_parse_refuses_a_bad_block_quietly():
    """<summary>No block, a missing colour, an unreadable one or a strength out of range all answer None.</summary>"""
    assert looks.parse("t", "T", None) is None
    assert looks.parse("t", "T", "tube") is None
    assert looks.parse("t", "T", {"ink": [1, 2, 3]}) is None
    assert looks.parse("t", "T", {"ink": "green", "black": [0, 0, 0]}) is None
    assert looks.parse("t", "T", {"ink": [1, 2, 3], "black": [0, 0, 0], "glow": 3}) is None
    assert looks.parse("t", "T", {"ink": [1, 2, 300], "black": [0, 0, 0]}) is None


def _ink_square(size: int, look: looks.Look) -> Image.Image:
    """<summary>A square of ink in the middle of the look's ground.</summary>
    <param name="size">The tile size.</param>
    <param name="look">The look whose colours to use.</param>
    <returns>An RGB tile.</returns>"""
    tile = images.solid(look.palette.background, size)
    ImageDraw.Draw(tile).rectangle([size * 0.35, size * 0.35, size * 0.65, size * 0.65], fill=look.palette.foreground)
    return tile


def test_finish_glows_falls_off_and_draws_scan_lines():
    """<summary>The three parts of the finish each leave their mark in the pixels.</summary>"""
    look = looks.parse("tube", "Tube", BLOCK)
    size = 285
    out = looks.finish(_ink_square(size, look), look)
    ground = look.palette.background
    # a halo: just outside the square the ground is lit a little
    beside = out.getpixel((int(size * 0.69), size // 2))
    assert beside[1] > ground[1] + 10
    # the corners fall away towards the ground, darker than the halo free middle edge
    assert sum(out.getpixel((2, 2))) <= sum(out.getpixel((size // 2, 2)))
    # scan lines: inside the square one row in every pitch is darker than the one above
    column = [out.getpixel((size // 2, y))[1] for y in range(int(size * 0.4), int(size * 0.6))]
    assert any(column[i] < column[i - 1] - 20 for i in range(1, len(column)))


def test_finish_parts_can_each_be_turned_off():
    """<summary>Zero glow, fall and lines leave the drawing as it was.</summary>"""
    look = looks.parse("flat", "Flat", {**BLOCK, "glow": 0, "fall": 0, "lines": 0})
    tile = _ink_square(95, look)
    assert list(looks.finish(tile, look).getdata()) == list(tile.getdata())


def test_render_plain_is_the_drawing_itself():
    """<summary>With no look the drawing runs at the size asked in the plain palette, untouched.</summary>"""
    seen = []

    def draw(size, p):
        """<summary>Records what it was asked for.</summary>"""
        seen.append((size, p))
        return images.solid(p.background, size)
    out = looks.render(draw, 95, None, None)
    assert out.size == (95, 95) and seen == [(95, looks.PLAIN)]
    out = looks.render(draw, 95, (1, 2, 3), None)
    assert seen[-1] == (95, looks.PLAIN.on((1, 2, 3))) and out.getpixel((0, 0)) == (1, 2, 3)


def test_render_themed_draws_large_and_shrinks():
    """<summary>With a look the drawing runs at three times the size and comes back at the size asked.</summary>"""
    look = looks.parse("tube", "Tube", BLOCK)
    seen = []

    def draw(size, p):
        """<summary>Records what it was asked for.</summary>"""
        seen.append((size, p))
        return _ink_square(size, look)
    out = looks.render(draw, 95, None, look)
    assert out.size == (95, 95) and seen == [(285, look.palette)]
    assert out.getpixel((47, 47))[1] > 200 and out.getpixel((47, 47))[0] < 160


def test_wrist_console_declares_its_look_and_the_others_do_not():
    """<summary>The shipped Wrist Console manifest carries a look in the tube's own colours; a theme of icons alone reports none.</summary>"""
    look = themes.look("wrist-console")
    assert look is not None and look.palette.foreground == (96, 255, 128) and look.palette.background == (5, 16, 9)
    assert look.palette.alert != look.palette.foreground
    assert themes.look("controls") is None
    assert themes.look("no-such-theme") is None
    assert themes.look("../wrist-console") is None
    flags = {t["slug"]: t["look"] for t in themes.list_themes()}
    assert flags["wrist-console"] is True and flags["controls"] is False


def test_every_drawn_tile_takes_the_look():
    """<summary>Each tile drawn from a reading comes out green on the tube when given the look, and as before without it.</summary>"""
    look = themes.look("wrist-console")
    now = datetime(2026, 11, 5, 14, 30)
    plain = tiles.clock_tile(now, 95)
    themed = tiles.clock_tile(now, 95, look=look)
    assert plain.getpixel((2, 2)) == images.BACKGROUND
    assert themed.getpixel((2, 2))[1] < 40 and themed.getpixel((2, 2)) != images.BACKGROUND
    for tile in (themed, tiles.date_tile(now, 95, look=look), tiles.weather_key_tile(None, 95, look=look),
                 tiles.system_tile(None, 95, look=look), tiles.lights_tile(None, 95, look=look),
                 tiles.pihole_tile(None, 95, look=look), tiles.synology_tile(None, 95, look=look),
                 tiles.label_tile("Deaths", 95, look=look)):
        assert tile.size == (95, 95)
        colours = [c for _, c in tile.getcolors(95 * 95) if c[1] > 60]
        assert colours and all(c[1] > c[0] and c[1] > c[2] for c in colours)
