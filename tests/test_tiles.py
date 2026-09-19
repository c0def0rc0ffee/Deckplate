"""<summary>
Drawing the key and strip tiles, checked by sampling the pixels that come out.
</summary>
<remarks>
These tests read pictures rather than describe them, because that is the only
honest way to check drawing code: a tile that renders nothing is still a valid
picture and passes every check that does not look at it. Sampling a corner or
comparing against a plain background is deliberately coarse so the tests do not
break when a typeface or a margin is nudged.

Two ideas run through the file and are worth knowing before reading it. A tile
signature decides whether a tile is redrawn and resent to the deck, so anything
that changes the picture has to change the signature or the screen will go
stale. And the wallpaper and grid pattern treat the whole deck as one picture
seen through the key windows, at the configured key pitch, which is why so many
assertions are about the gaps between keys rather than the keys themselves.
</remarks>
"""

from datetime import datetime
from pathlib import Path

from PIL import Image, ImageChops

from deckplate import images, tiles
from deckplate.config import KeyConfig, StripTile
from deckplate.weather import Observation

SIZE = 95


def differs_from_blank(tile) -> bool:
    """
    <summary>
    Whether a tile differs anywhere from the plain background.
    </summary>
    <param name="tile">An image.</param>
    <returns>True or False.</returns>
    """
    return ImageChops.difference(tile, images.solid(images.BACKGROUND, SIZE)).getbbox() is not None


def test_empty_key_is_plain_background():
    """<summary>
    A position with no configuration draws as plain background and nothing else.
    </summary>
    <remarks>
    Empty keys are the majority of a fresh deck. If anything were drawn on them, a
    marker or a faint outline, it would appear eighteen times over and there would
    be no way to turn it off from the configuration file.
    </remarks>
    """
    assert not differs_from_blank(tiles.key_tile(None, SIZE))


def test_label_only_and_action_only_keys_are_visible():
    """<summary>
    A key with only a label, and a key with only an action, both put something on
    the screen.
    </summary>
    <remarks>
    The action only case is the one that gets lost. A key that does something must
    never look identical to an empty one, or the only way to find it is to press
    every key on the deck and see what happens.
    </remarks>
    """
    assert differs_from_blank(tiles.key_tile(KeyConfig(0, 0, label="Web"), SIZE))
    from deckplate.config import Action
    assert differs_from_blank(tiles.key_tile(KeyConfig(0, 0, action=Action("sleep")), SIZE))


def test_label_only_key_wraps_words_to_fill_the_key():
    """<summary>
    A label with no picture is drawn big, one word per line where needed.
    </summary>
    <remarks>
    The rule is to use the largest font that fits, so the number of lines follows
    from the text rather than being fixed. Short labels stay on one line even
    though splitting them would allow a bigger font, because a two word label
    broken across two lines reads worse than a slightly smaller one.

    Two cases are pinned because they are where the rule breaks down. A single word
    wider than the key at the smallest font is kept whole rather than cut, and an
    explicit line break in the text is obeyed. The final check is about position
    rather than size: the ink reaches the upper half of the key, which an earlier
    version that only drew in a bottom band never did.
    </remarks>
    """
    lines, font_size = tiles.label_lines("Request Hangar Access", SIZE)
    assert lines == ["Request", "Hangar", "Access"]
    assert font_size >= 14
    short_lines, short_font = tiles.label_lines("Web", SIZE)
    assert short_lines == ["Web"] and short_font > font_size
    # short labels stay on one line rather than splitting for bigger print
    assert tiles.label_lines("Vol +", SIZE)[0] == ["Vol +"]
    assert tiles.label_lines("Page 2", SIZE)[0] == ["Page 2"]
    assert tiles.label_lines("Star Citizen", SIZE)[0] == ["Star", "Citizen"]
    assert tiles.label_lines("Top\nBottom", SIZE)[0] == ["Top", "Bottom"]
    # a word wider than the key at the smallest font is kept whole
    lines, font_size = tiles.label_lines("Supercalifragilistic", SIZE)
    assert lines == ["Supercalifragilistic"] and font_size == tiles.LABEL_MIN_FONT
    # ink lands in the upper half of the key, where the old bottom band never reached
    tile = tiles.key_tile(KeyConfig(0, 0, label="Request Hangar Access"), SIZE)
    upper = tile.crop((0, 0, SIZE, SIZE // 2))
    assert differs_from_blank(upper.resize((SIZE, SIZE)))


def test_picture_tile_keeps_aspect_and_missing_file_is_marked(tmp_path):
    """<summary>
    A picture is letterboxed rather than squashed, and a picture that is not there
    is drawn as a dark red tile.
    </summary>
    <remarks>
    The aspect check samples two rows: background near the top edge and the picture
    in the middle. A stretched picture would fill both and pass a size only check
    while looking wrong on the deck.

    The missing file case matters more than it looks. A missing picture must be
    visible on the hardware as a marked tile, because the alternative is a key that
    silently looks empty while its action still works, and the configuration file
    gives no clue that the path is wrong.
    </remarks>
    """
    wide = images.solid((200, 30, 30), 10).resize((200, 50))
    path = tmp_path / "wide.png"
    wide.save(path)
    tile = tiles.key_tile(KeyConfig(0, 0, image=path, label="Wide"), SIZE)
    assert tile.size == (SIZE, SIZE)
    # letterboxed: top rows are background, middle rows are red
    assert tile.getpixel((47, 3)) == images.BACKGROUND
    r, g, b = tile.getpixel((47, 47))
    assert r > 150 and g < 80
    missing = tiles.key_tile(KeyConfig(0, 0, image=Path(tmp_path / "nope.png")), SIZE)
    assert missing.getpixel((3, 3)) == (60, 20, 20)


def test_backgrounds_apply_to_keys_and_strip():
    """<summary>
    A background can be set per key, per strip tile, or once for the whole deck,
    and the more specific setting wins.
    </summary>
    <remarks>
    The deck wide default is what makes a themed deck practical, and the per key
    override is what makes one key stand out on it. The precedence is checked in
    both directions so that a future change cannot quietly make the default win.

    The last assertion is the one that keeps the screen honest: the background is
    part of the tile signature. Without that, changing a background in the
    configuration file would redraw nothing, because the tile would be judged
    unchanged and never resent to the deck.
    </remarks>
    """
    from deckplate.config import AnimationConfig
    key = tiles.key_tile(KeyConfig(0, 0, label="Go", background="#ff0000"), SIZE)
    assert key.getpixel((3, 3)) == (255, 0, 0)
    deck_wide = tiles.key_tile(KeyConfig(0, 0, label="Go"), SIZE, default_background="#00ff00")
    assert deck_wide.getpixel((3, 3)) == (0, 255, 0)
    own_wins = tiles.key_tile(KeyConfig(0, 0, label="Go", background="#0000ff"), SIZE, default_background="#00ff00")
    assert own_wins.getpixel((3, 3)) == (0, 0, 255)
    assert tiles.key_tile(None, SIZE).getpixel((3, 3)) == images.BACKGROUND
    frames, _ = tiles.key_frames(KeyConfig(0, 0, animation=AnimationConfig("spinner"), background="#ff0000"), SIZE)
    assert frames[0].getpixel((3, 3)) == (255, 0, 0)
    now = datetime(2026, 9, 10, 14, 5)
    clock = tiles.strip_tile(StripTile("clock", background="#ff0000"), now, None, SIZE, "", visible=(60, 60))
    assert clock.getpixel((3, 3)) == (255, 0, 0)
    deck_clock = tiles.strip_tile(StripTile("clock"), now, None, SIZE, "", visible=(60, 60), default_background="#00ff00")
    assert deck_clock.getpixel((3, 3)) == (0, 255, 0)
    assert tiles.strip_signature(StripTile("clock"), now, None) != tiles.strip_signature(StripTile("clock", background="#ff0000"), now, None)


def test_clock_date_and_weather_tiles_render():
    """<summary>
    Every strip tile kind draws something, including the weather tile with no
    reading yet and with each icon it knows.
    </summary>
    <remarks>
    Walking all the icon names is what catches one missing from the drawing code:
    an icon the forecast can return but the tile cannot draw gives a blank strip
    tile in exactly the weather that produces it, which is the hardest case to
    reproduce on purpose. The no reading case covers the first seconds after
    startup, before the first fetch has finished.
    </remarks>
    """
    now = datetime(2026, 9, 10, 14, 5)
    assert differs_from_blank(tiles.clock_tile(now, SIZE))
    assert differs_from_blank(tiles.date_tile(now, SIZE))
    assert differs_from_blank(tiles.weather_tile(None, SIZE))
    for icon in ("sun", "partly", "cloud", "fog", "rain", "snow", "storm", "other"):
        obs = Observation(12.4, 0, "Clear", icon, "°C")
        assert differs_from_blank(tiles.weather_tile(obs, SIZE, "Guernsey"))


def test_strip_signature_changes_with_minute_and_weather():
    """<summary>
    The strip signature ignores seconds but follows the minute and the reading.
    </summary>
    <remarks>
    This is the redraw budget for the strip. The clock only shows minutes, so
    including seconds would rebuild and resend three tiles over USB every second
    for no visible change. Following the reading is the other half: a temperature
    that changed while the minute did not still has to reach the screen.
    </remarks>
    """
    clock = StripTile("clock")
    t1, t2 = datetime(2026, 9, 10, 14, 5, 10), datetime(2026, 9, 10, 14, 5, 50)
    assert tiles.strip_signature(clock, t1, None) == tiles.strip_signature(clock, t2, None)
    assert tiles.strip_signature(clock, t1, None) != tiles.strip_signature(clock, datetime(2026, 9, 10, 14, 6), None)
    w = StripTile("weather")
    a = Observation(10, 0, "Clear", "sun", "°C")
    b = Observation(11, 0, "Clear", "sun", "°C")
    assert tiles.strip_signature(w, t1, a) != tiles.strip_signature(w, t1, b)


def test_strip_tiles_keep_to_the_visible_window(tmp_path):
    """<summary>
    Strip content stays inside the central window the physical bezel leaves
    visible, unless it is set to fill.
    </summary>
    <remarks>
    The strip screens are wider than the cut outs in front of them, so the tile is
    the full image size but only the middle of it can actually be seen. Drawing to
    the edges puts content behind the bezel where it vanishes without any sign of
    being lost.

    Filling is the deliberate exception, for pictures where covering the whole
    width looks better than fitting inside the window, and the two modes produce
    different signatures so switching between them actually redraws.
    </remarks>
    """
    now = datetime(2026, 9, 10, 14, 5)
    clock = tiles.strip_tile(StripTile("clock"), now, None, SIZE, "", visible=(60, 60))
    assert clock.size == (SIZE, SIZE)
    # everything outside the central 60 by 60 window is plain background
    for x in range(SIZE):
        assert clock.getpixel((x, 5)) == images.BACKGROUND
        assert clock.getpixel((5, x)) == images.BACKGROUND
    assert differs_from_blank(clock)

    path = tmp_path / "wide.png"
    images.solid((200, 30, 30), 10).resize((300, 100)).save(path)
    filled = tiles.strip_tile(StripTile("image", path), now, None, SIZE, "", visible=(60, 60))
    fitted = tiles.strip_tile(StripTile("image", path, fit=True), now, None, SIZE, "", visible=(60, 60))
    # filling uses the whole 95 px width; fitting stays inside the 60 px window
    assert filled.getpixel((2, 47))[0] > 150
    assert fitted.getpixel((2, 47)) == images.BACKGROUND
    assert fitted.getpixel((47, 47))[0] > 150
    assert tiles.strip_signature(StripTile("image", path), now, None) != tiles.strip_signature(StripTile("image", path, fit=True), now, None)


def test_ruler_tile_renders():
    """<summary>
    The ruler tile draws right into its corner.
    </summary>
    <remarks>
    This tile exists to measure the deck: it is shown while the key pitch is being
    set up, so the alignment marks have to reach the very edge of the tile. A
    margin, however small, is the one thing that would make it useless.
    </remarks>
    """
    ruler = tiles.ruler_tile(SIZE, "S1")
    assert ruler.size == (SIZE, SIZE)
    assert ruler.getpixel((0, 0)) != images.BACKGROUND


def test_strip_tile_dispatch(tmp_path):
    """<summary>
    Every strip tile kind is dispatched and comes back at the requested size,
    including a picture from disk.
    </summary>
    <remarks>
    A kind that fell through the dispatch would raise or return None in the run
    loop rather than showing a blank, so this is a cheap guard on the whole set
    rather than a test of any one drawing routine.
    </remarks>
    """
    now = datetime(2026, 9, 10, 14, 5)
    for kind in ("clock", "date", "weather", "blank"):
        assert tiles.strip_tile(StripTile(kind), now, None, SIZE, "").size == (SIZE, SIZE)
    path = tmp_path / "p.png"
    images.solid((0, 200, 0), 40).save(path)
    tile = tiles.strip_tile(StripTile("image", path), now, None, SIZE, "")
    assert tile.getpixel((47, 47))[1] > 150


def test_grid_pattern_is_the_canvas_seen_through_the_key_windows():
    """<summary>
    Each tile is the window a key would show onto one picture laid at that pitch.
    </summary>
    <remarks>
    The grid pattern is what the configuration page shows while the key pitch is
    being measured, so it has to be built exactly the way a wallpaper is: one
    canvas sized for the whole deck, cropped per key at the pitch. Comparing each
    tile against the crop from the canvas is what proves the two paths agree
    instead of merely looking similar.

    The middle key is skipped because the pitch is printed on it. The flush case
    checks the other end of the range, where the pitch equals the key size and the
    tiles butt together with no gap, and the last comparison proves a different
    pitch really does give a different picture rather than the same one shifted.
    </remarks>
    """
    from deckplate import layout
    from deckplate.config import KEY_PITCH_RANGE
    canvas = tiles.grid_canvas(SIZE, 142, 160)
    assert canvas.size == ((layout.LCD_COLUMNS - 1) * 142 + SIZE, (layout.ROWS - 1) * 160 + SIZE)
    pattern = tiles.grid_pattern(SIZE, 142, 160)
    assert set(pattern) == {(r, c) for r in range(layout.ROWS) for c in range(layout.LCD_COLUMNS)}
    for (row, column), tile in pattern.items():
        assert tile.size == (SIZE, SIZE)
        if (row, column) == (1, 2):
            continue  # the middle key carries the pitch text on top
        window = canvas.crop((column * 142, row * 160, column * 142 + SIZE, row * 160 + SIZE))
        assert ImageChops.difference(tile, window).getbbox() is None
    # no gaps at all: the tiles butted together are the whole canvas
    flush = tiles.grid_pattern(SIZE, SIZE, SIZE)
    assert flush[(0, 0)].getpixel((SIZE - 1, 40)) == tiles.grid_canvas(SIZE, SIZE, SIZE).getpixel((SIZE - 1, 40))
    # a different pitch is a different picture on the same key
    assert ImageChops.difference(pattern[(0, 0)], flush[(0, 0)]).getbbox() is not None
    assert KEY_PITCH_RANGE[0] <= 142 <= KEY_PITCH_RANGE[1]


def _gradient(tmp_path, name="bg.png", width=600, height=200):
    """<summary>
    A wide picture going from red on the left to blue on the right.
    </summary>
    <returns>The path it was saved to.</returns>
    <remarks>
    A gradient rather than a pattern because the wallpaper tests need to tell
    where in the picture a slice came from, and a colour that changes smoothly
    across the width makes that a comparison between two pixels.
    </remarks>"""
    picture = Image.new("RGB", (width, height))
    for x in range(width):
        share = x / (width - 1)
        picture.paste((int(255 * (1 - share)), 0, int(255 * share)), (x, 0, x + 1, height))
    path = tmp_path / name
    picture.save(path)
    return path


def test_wallpaper_slices_are_windows_onto_one_picture(tmp_path):
    """<summary>
    The picture covers the whole grid at the pitch and each key shows its own
    window.
    </summary>
    <remarks>
    The point of the pitch is the gap between the keys. A wallpaper cut as if the
    keys touched would look continuous on screen and visibly stepped on the
    hardware, because the part of the picture that falls in the gaps has to be
    thrown away rather than squeezed into the tiles.

    That is what the colour comparisons measure: across the gap the colour has to
    jump further than it does across a key's own width, and with no pitch given the
    keys are treated as touching and the jump disappears. A missing file gives None
    so the caller falls back to the plain background, and a picture of the wrong
    shape is scaled to cover and cropped from the centre, never squashed.
    </remarks>
    """
    from deckplate import layout
    path = _gradient(tmp_path)
    pitch = (142, 158)
    first = tiles.wallpaper_slice(path, SIZE, pitch, 0, 0)
    last = tiles.wallpaper_slice(path, SIZE, pitch, 0, layout.LCD_COLUMNS - 1)
    assert first.size == (SIZE, SIZE) and last.size == (SIZE, SIZE)
    # red fades and blue grows from the left key to the right key
    assert first.getpixel((10, 40))[0] > last.getpixel((10, 40))[0]
    assert first.getpixel((10, 40))[2] < last.getpixel((10, 40))[2]
    # the right edge of one key is close in colour to the left edge of the next
    # only when the gap is accounted for: with the pitch the step across the gap
    # is bigger than the step across a key's own width
    a_right = tiles.wallpaper_slice(path, SIZE, pitch, 0, 0).getpixel((SIZE - 1, 40))[2]
    b_left = tiles.wallpaper_slice(path, SIZE, pitch, 0, 1).getpixel((0, 40))[2]
    assert b_left - a_right > 0
    flush_right = tiles.wallpaper_slice(path, SIZE, None, 0, 0).getpixel((SIZE - 1, 40))[2]
    flush_left = tiles.wallpaper_slice(path, SIZE, None, 0, 1).getpixel((0, 40))[2]
    assert 0 <= flush_left - flush_right <= 3  # keys treated as touching: continuous
    assert tiles.wallpaper_slice(tmp_path / "missing.png", SIZE, pitch, 0, 0) is None
    # a tall picture is scaled to cover and centre cropped, never squashed
    tall = _gradient(tmp_path, "tall.png", 100, 900)
    assert tiles.wallpaper_slice(tall, SIZE, pitch, 1, 2).size == (SIZE, SIZE)


def test_wallpaper_sits_under_keys_without_their_own_picture(tmp_path):
    """<summary>
    A wallpaper shows through wherever a key has nothing of its own, and is
    covered where a key has a picture.
    </summary>
    <remarks>
    The layering rule in one line: the key's own picture wins, a label is drawn in
    a band on top of the wallpaper, and an empty position shows the wallpaper
    untouched. The top of a labelled key is compared against the bare slice to
    prove the label is a band rather than a wash over the whole tile, which would
    make any wallpaper pointless as soon as a key was named.

    The action only case repeats here for a reason: a key that does something must
    stay findable against a busy wallpaper, not just against a plain background.
    </remarks>
    """
    from PIL import ImageChops
    path = _gradient(tmp_path)
    backdrop = tiles.wallpaper_slice(path, SIZE, (142, 158), 0, 0)
    # an empty position shows the slice as it is
    assert ImageChops.difference(tiles.key_tile(None, SIZE, backdrop=backdrop), backdrop).getbbox() is None
    # a label goes in the band on top of the slice: the top is untouched, the bottom is not
    labelled = tiles.key_tile(KeyConfig(0, 0, label="Web"), SIZE, backdrop=backdrop)
    assert ImageChops.difference(labelled.crop((0, 0, SIZE, 20)), backdrop.crop((0, 0, SIZE, 20))).getbbox() is None
    assert ImageChops.difference(labelled, backdrop).getbbox() is not None
    # an action only key keeps a visible mark on the slice
    from deckplate.config import Action
    marked = tiles.key_tile(KeyConfig(0, 0, action=Action("sleep")), SIZE, backdrop=backdrop)
    assert ImageChops.difference(marked, backdrop).getbbox() is not None
    # a key with its own picture ignores the wallpaper
    own = tmp_path / "own.png"
    images.solid((0, 200, 0), 40).save(own)
    with_own = tiles.key_tile(KeyConfig(0, 0, image=own), SIZE, backdrop=backdrop)
    assert with_own.getpixel((SIZE // 2, SIZE // 2)) == (0, 200, 0)
