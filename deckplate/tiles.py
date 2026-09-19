"""<summary>
Draw what goes on each panel: pictures with labels, clock, date, weather, and
the calibration patterns used to work out what a panel really shows.
</summary>
<remarks>
Everything returns an upright PIL image of the key size. Orientation for the
hardware is applied later by images.encode, so nothing in this file should
ever think about the panels being mounted sideways.

Two sorts of panel are drawn here and they are not interchangeable. A key is a
plain square. A strip panel is sent a square as well, but shows only a central
window of it, so its tiles are drawn small and centred with
<see cref="centred"/>, and a tile drawn at full size for a strip loses its
edges. The ``visible`` argument is what carries that window size in.

Nothing here reads the clock, the network or the config. Times, readings and
settings all arrive as arguments, which is what lets every tile be rendered in
a test at a fixed moment and compared.
</remarks>
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

from . import animations, images
from .config import AnimationConfig, KeyConfig, StripTile
from .weather import Observation

# The label strip along the bottom of a picture tile. RGBA, and the only
# translucent colour here: it is composited over the picture rather than drawn
# into it, so the picture still shows through behind the text.
BAND = (0, 0, 0, 150)
SUN = (255, 200, 60)
CLOUD = (215, 222, 230)
RAIN = (110, 170, 240)
SNOW = (240, 245, 255)
BOLT = (255, 220, 80)
MUTED = (150, 165, 180)


# The default running border, drawn round a key whose hold is currently on.
# A key config can override the colour, so this is the fallback rather than
# the rule.
HOLDING = (76, 175, 125)


def key_background(key: KeyConfig | None, default: str | None = None) -> tuple[int, int, int]:
    """<summary>
    The background colour for one key: its own, else the deck wide default,
    else the built in navy.
    </summary>
    <param name="key">The key's config, or None for an empty position.</param>
    <param name="default">The page or deck wide background, as written.</param>
    <returns>An RGB tuple, always. Never None.</returns>
    <remarks>
    Both steps fall back silently, because <see cref="images.colour"/> turns
    unreadable text into the fallback rather than raising. A key that comes
    out navy when it should not has a colour the parser could not read, and
    the config is where to look, not here.
    </remarks>
    """
    if key is not None and key.background:
        return images.colour(key.background)
    return images.colour(default)


def key_tile(key: KeyConfig | None, size: int, active: bool = False,
             default_background: str | None = None,
             backdrop: Image.Image | None = None,
             mark_colour: tuple[int, int, int] | None = None) -> Image.Image:
    """<summary>
    The still picture for one key, whatever the key is configured to show.
    </summary>
    <param name="key">The key's config, or None for a position with nothing
    on it.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <param name="active">True marks a hold that is currently on, which draws
    the running border.</param>
    <param name="default_background">The page or deck wide background colour,
    used when the key names none of its own.</param>
    <param name="backdrop">This key's slice of the page's wallpaper, or None.
    It stands in for the background under a key with no picture of its own,
    so a label goes in the band on top of it as it would on a picture. A key
    with its own picture keeps that instead.</param>
    <param name="mark_colour">The running border's colour, or None for the
    built in one.</param>
    <returns>An upright tile of ``size`` square.</returns>
    <remarks>
    The order the cases are tried in is the behaviour: a picture wins over
    the wallpaper, the wallpaper wins over a label drawn large, and a label
    drawn large wins over a plain background. Reordering them changes what
    the deck shows for configs that set more than one.
    A key that is an action but shows nothing at all still gets an outline,
    so a mistyped picture path leaves a visibly live key rather than a gap
    that looks like a dead position.
    This is the still path only. A key with an animation or an animated
    picture file goes through <see cref="key_frames"/> instead, and calling
    this on one gives its first frame at best.
    </remarks>
    """
    background = key_background(key, default_background)
    if key is None:
        return backdrop.copy() if backdrop is not None else images.solid(background, size)
    if key.image is not None:
        tile = picture_tile(key.image, size, background)
        if key.label:
            tile = with_label(tile, key.label)
    elif backdrop is not None:
        tile = backdrop.copy()
        if key.label:
            tile = with_label(tile, key.label)
        else:
            # an action with no picture and no label still gets a visible mark
            draw = ImageDraw.Draw(tile)
            draw.rectangle([0, 0, size - 1, size - 1], outline=images.ACCENT, width=2)
    elif key.label:
        # No picture: the label is the picture. Big, wrapped, filling the key.
        tile = label_tile(key.label, size, background)
    else:
        tile = images.solid(background, size)
        # an action with no picture and no label still gets a visible mark
        draw = ImageDraw.Draw(tile)
        draw.rectangle([0, 0, size - 1, size - 1], outline=images.ACCENT, width=2)
    if active:
        _draw_mark(tile, mark_colour or HOLDING)
    return tile


def _animation(config: AnimationConfig) -> animations.Animation:
    """
    <summary>
    An Animation built from its config.
    </summary>
    <param name="config">The animation config.</param>
    <returns>An Animation.</returns>
    """
    return animations.Animation(config.kind, config.colour, config.speed, config.fps)


def _draw_mark(tile: Image.Image, colour: tuple[int, int, int]) -> Image.Image:
    """<summary>
    Draw the running border and its corner dot onto a tile, in place.
    </summary>
    <param name="tile">The tile to mark. It is modified, not copied.</param>
    <param name="colour">RGB tuple for the border and the dot.</param>
    <returns>The same tile, for chaining.</returns>
    <remarks>
    In place on purpose, so a whole animation can be marked without doubling
    the memory it holds. That means the caller must own its frames: marking a
    frame handed straight out of the animation cache would stain every later
    use of it, which is why <see cref="animations.frames"/> returns copies.
    </remarks>"""
    size = tile.width
    draw = ImageDraw.Draw(tile)
    draw.rectangle([0, 0, size - 1, size - 1], outline=colour, width=4)
    r = max(4, size // 12)
    draw.ellipse([size - 3 * r, r, size - r, 3 * r], fill=colour)
    return tile


def key_frames(key: KeyConfig | None, size: int, active: bool = False,
               default_background: str | None = None,
               mark_colour: tuple[int, int, int] | None = None) -> tuple[list[Image.Image], int] | None:
    """<summary>
    The frames and rate of an animated key, or None when the key is static.
    </summary>
    <param name="key">The key's config, or None.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <param name="active">True to draw the running border on every frame.</param>
    <param name="default_background">The page or deck wide background colour.</param>
    <param name="mark_colour">The running border's colour, or None for the
    built in one.</param>
    <returns>The frames and the frame rate, or None to say the caller should
    use <see cref="key_tile"/> instead.</returns>
    <remarks>
    None is the normal answer for most keys and is not an error. Ask here
    first and fall back to the still tile: that is the order the controller
    uses and the only way to tell the two sorts of key apart.
    A built in animation replaces the picture; an animated picture file plays
    as it is. The label and the running mark go on every frame, except that a
    scrolling animation is already made of the label and so is left alone.
    A picture file that turns out to be unreadable gives None rather than
    raising, which lands the key on the still path and draws the missing
    tile there.
    There is no wallpaper here. An animated key covers its own square every
    frame, so a page wallpaper would never be seen behind it.
    </remarks>
    """
    if key is None:
        return None
    background = key_background(key, default_background)
    if key.animation is not None:
        frames = animations.frames(_animation(key.animation), size, key.label or "", background)
        fps = key.animation.fps
        if key.label and key.animation.kind != "scroll":
            frames = [with_label(frame, key.label) for frame in frames]
    elif key.image is not None and animations.is_animated_file(key.image):
        try:
            frames, fps = animations.file_frames(key.image, size, background)
        except (OSError, ValueError):
            return None
        if key.label:
            frames = [with_label(frame, key.label) for frame in frames]
    else:
        return None
    if active:
        frames = [_draw_mark(frame, mark_colour or HOLDING) for frame in frames]
    return frames, fps


def strip_background(tile: StripTile, default: str | None = None) -> tuple[int, int, int]:
    """<summary>
    The background colour for one strip panel: its own, else the deck wide
    default.
    </summary>
    <param name="tile">The strip tile's config.</param>
    <param name="default">The page or deck wide background, as written.</param>
    <returns>An RGB tuple, always.</returns>
    <remarks>
    This colour does double duty on a strip: it fills the panel's own window
    and also the margin round it that the panel is sent but never shows, so
    that a tile which is a pixel out does not show a hard edge.
    </remarks>
    """
    return images.colour(tile.background) if tile.background else images.colour(default)


def strip_frames(tile: StripTile, size: int, visible: tuple[int, int] | None = None,
                 default_background: str | None = None) -> tuple[list[Image.Image], int] | None:
    """<summary>
    The frames and rate of an animated strip panel, or None when it is
    static.
    </summary>
    <param name="tile">The strip tile's config.</param>
    <param name="size">The square the panel is sent, in pixels.</param>
    <param name="visible">The window the panel actually shows, in pixels, or
    None to treat the whole square as visible.</param>
    <param name="default_background">The page or deck wide background colour.</param>
    <returns>The frames and the frame rate, or None for a static panel.</returns>
    <remarks>
    The frames are rendered at the smaller of the visible window's two sides
    and then centred in the full square, so the animation stays inside what
    the panel shows. Rendering at ``size`` instead is the mistake that makes
    an animation look cropped on the strip but correct in the preview.
    A picture file that cannot be read gives None, which puts the panel on
    the still path.
    </remarks>
    """
    width, height = visible if visible else (size, size)
    width, height = min(width, size), min(height, size)
    inner = min(width, height)
    background = strip_background(tile, default_background)
    if tile.kind == "animation" and tile.animation is not None:
        frames = animations.frames(_animation(tile.animation), inner, "", background)
        return [centred(frame, size, background) for frame in frames], tile.animation.fps
    if tile.kind == "image" and tile.image is not None and animations.is_animated_file(tile.image):
        try:
            frames, fps = animations.file_frames(tile.image, inner if tile.fit else size, background)
        except (OSError, ValueError):
            return None
        if tile.fit:
            frames = [centred(frame, size, background) for frame in frames]
        return frames, fps
    return None


def flash_tile(tile: Image.Image) -> Image.Image:
    """<summary>
    The pressed look: the tile brightened with a light border round it.
    </summary>
    <param name="tile">The tile as it normally looks.</param>
    <returns>A new tile. The original is not changed.</returns>
    <remarks>
    Brightening rather than replacing means this works on any tile, picture
    or label or animation frame alike, with no knowledge of what is on it.
    A tile that is already near white barely changes, which is what the
    border is there to cover.
    Shown for a moment and then replaced by the normal tile, so whatever
    draws this owes the deck the redraw that puts it back.
    </remarks>
    """
    bright = ImageEnhance.Brightness(tile).enhance(1.7)
    draw = ImageDraw.Draw(bright)
    draw.rectangle([0, 0, bright.width - 1, bright.height - 1], outline=images.FOREGROUND, width=3)
    return bright


def picture_tile(path: Path, size: int, background: tuple[int, int, int] = images.BACKGROUND) -> Image.Image:
    """<summary>
    A picture file scaled to fit one key, centred on the background with the
    aspect ratio kept.
    </summary>
    <param name="path">The picture file.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <param name="background">RGB tuple to letterbox against.</param>
    <returns>An upright tile of ``size`` square, always. Never None.</returns>
    <remarks>
    An unreadable file draws <see cref="missing_tile"/> instead of raising,
    because a page with one bad path in it should come up with one obviously
    wrong key rather than not come up at all.
    This is the step that keeps a picture from being squashed: the encoder
    squares whatever it is given without regard for the aspect ratio, so a
    picture that reaches it without coming through here comes out stretched.
    Scaling is down only. A picture smaller than the key is centred at its
    own size rather than blown up.
    </remarks>
    """
    tile = images.solid(background, size)
    try:
        with Image.open(path) as source:
            picture = source.convert("RGBA")
    except (OSError, ValueError):
        return missing_tile(path, size)
    picture.thumbnail((size, size), Image.Resampling.LANCZOS)
    offset = ((size - picture.width) // 2, (size - picture.height) // 2)
    tile.paste(picture, offset, picture)
    return tile


def missing_tile(path: Path, size: int) -> Image.Image:
    """<summary>
    The stand in for a picture that could not be read: a dark red tile with a
    question mark and the file name.
    </summary>
    <param name="path">The file that failed. Only its name is shown.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <returns>An upright tile.</returns>
    <remarks>
    Loud on purpose. A missing picture is a config fault the user can fix,
    and it should be obvious on the deck at a glance rather than looking like
    an empty key.
    Only the file name goes on the tile, never the full path, so a photograph
    of the deck never carries the folders it came from.
    </remarks>
    """
    tile = images.solid((60, 20, 20), size)
    draw = ImageDraw.Draw(tile)
    draw.text((size / 2, size / 2), "?", fill=images.FOREGROUND, font=images.load_font(int(size * 0.5)), anchor="mm")
    return with_label(tile, path.name)


LABEL_MAX_LINES = 4
LABEL_LINE_SPACING = 1.15
LABEL_MIN_FONT = 9
# The fewest lines win as long as the text stays at least this share of the
# key tall; below it, another line is better than smaller print.
LABEL_COMFORTABLE = 0.18


def _wrap(paragraphs: list[list[str]], font, width: int, probe: ImageDraw.ImageDraw) -> list[str]:
    """<summary>
    Greedy word wrap of each paragraph at one fixed font.
    </summary>
    <param name="paragraphs">Each paragraph already split into words. An
    empty paragraph is skipped rather than becoming a blank line.</param>
    <param name="font">The font to measure with, which must be the one the
    text will actually be drawn in.</param>
    <param name="width">The width to wrap to, in pixels.</param>
    <param name="probe">A throwaway draw handle used only for measuring.</param>
    <returns>The wrapped lines, in order.</returns>
    <remarks>
    A single word wider than ``width`` is kept whole and put on its own line
    rather than being broken, so the answer can be wider than asked for. The
    caller is expected to check the result actually fits, which is what
    <see cref="label_lines"/> does when it tries each font size.
    </remarks>
    """
    lines: list[str] = []
    for words in paragraphs:
        if not words:
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            left, _, right, _ = probe.textbbox((0, 0), candidate, font=font)
            if right - left <= width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def label_lines(label: str, size: int) -> tuple[list[str], int]:
    """<summary>
    Wrap a label into the lines and font size that best fill a key.
    </summary>
    <param name="label">The text as the user typed it.</param>
    <param name="size">The key's pixel size; the page passes a multiple of it.</param>
    <returns>The lines and the font size that fits them.</returns>
    <remarks>
    Words wrap onto new lines and a newline in the label forces a break. The
    fewest lines win while the font stays at least LABEL_COMFORTABLE of the key
    tall, so "Vol +" stays on one line rather than splitting for the sake of
    bigger print; past that, more lines beat smaller print, so "Request Hangar
    Access" goes one word per line. Never more than LABEL_MAX_LINES and never
    below LABEL_MIN_FONT. A single word wider than the key at the smallest
    font is kept whole and drawn clipped rather than split.
    Both the text and a literal backslash n in it are treated as a line
    break, because a config file written by hand is where these labels come
    from and both spellings turn up.
    Measuring is the cost here: every font size tried wraps the whole label
    again. It is called once per key when a page is prepared, never while the
    deck is running.
    </remarks>
    """
    pad = max(3, size // 16)
    width = size - 2 * pad
    height = size - 2 * pad
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    paragraphs = [p.split() for p in label.replace("\\n", "\n").split("\n")]
    if not any(paragraphs):
        return [], LABEL_MIN_FONT
    largest = max(LABEL_MIN_FONT, int(size * 0.34))
    comfortable = max(LABEL_MIN_FONT, int(size * LABEL_COMFORTABLE))

    def best_for(max_lines: int) -> tuple[list[str], int] | None:
        """<summary>
        The largest font whose wrap fits within ``max_lines`` lines, if any.
        </summary>
        <param name="max_lines">The most lines allowed.</param>
        <returns>The lines and font size, or None when nothing fits.</returns>
        <remarks>
        Walks down from the largest font one pixel at a time and takes the
        first that fits, so it is linear in the font range rather than clever.
        At these sizes that is a few dozen measurements and is not worth
        bisecting: the result would be the same and harder to follow.
        Three things have to hold before a size is accepted: the line count,
        the total height, and every single line fitting the width, because
        the wrap keeps an over wide word whole rather than breaking it.
        </remarks>
        """
        for font_size in range(largest, LABEL_MIN_FONT - 1, -1):
            font = images.load_font(font_size)
            lines = _wrap(paragraphs, font, width, probe)
            if len(lines) > max_lines or len(lines) * font_size * LABEL_LINE_SPACING > height:
                continue
            if any(probe.textbbox((0, 0), line, font=font)[2] > width for line in lines):
                continue
            return lines, font_size
        return None

    fallback: tuple[list[str], int] | None = None
    for max_lines in range(1, LABEL_MAX_LINES + 1):
        found = best_for(max_lines)
        if found is None:
            continue
        if found[1] >= comfortable:
            return found
        if fallback is None or found[1] > fallback[1]:
            fallback = found
    if fallback is not None:
        return fallback
    # nothing fits even at the smallest font: one word too wide, kept whole
    font = images.load_font(LABEL_MIN_FONT)
    return _wrap(paragraphs, font, width, probe), LABEL_MIN_FONT


def label_tile(label: str, size: int, background: tuple[int, int, int] = images.BACKGROUND) -> Image.Image:
    """<summary>
    The label alone, wrapped and centred, filling the whole face of a key.
    </summary>
    <param name="label">The text as the user typed it.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <param name="background">RGB tuple behind the text.</param>
    <returns>An upright tile.</returns>
    <remarks>
    This is what a key with no picture looks like, and it is deliberately
    different from <see cref="with_label"/>: there the label is a caption in
    a band over a picture, here it is the picture.
    A label that is nothing but whitespace gives a plain background rather
    than an empty box, which is the same thing an empty label does.
    </remarks>
    """
    tile = images.solid(background, size)
    lines, font_size = label_lines(label, size)
    if not lines:
        return tile
    draw = ImageDraw.Draw(tile)
    font = images.load_font(font_size)
    step = font_size * LABEL_LINE_SPACING
    top = size / 2 - step * (len(lines) - 1) / 2
    for index, line in enumerate(lines):
        draw.text((size / 2, top + index * step), line, fill=images.FOREGROUND, font=font, anchor="mm")
    return tile


def with_label(tile: Image.Image, label: str) -> Image.Image:
    """<summary>
    Put a translucent band along the bottom of a tile with the label in it.
    </summary>
    <param name="tile">The picture to caption. It is not modified.</param>
    <param name="label">The caption text.</param>
    <returns>A new RGB tile the same size.</returns>
    <remarks>
    One line only, shrunk to fit and never wrapped: the band is a caption on
    a picture, not the face of the key, and a long name simply comes out
    small. Where the label should be the whole tile, use
    <see cref="label_tile"/>.
    The band is composited rather than drawn, so the picture still shows
    faintly through it. That is what keeps a dark picture from looking like
    it has a solid block cut out of it.
    Comes back as RGB whatever went in, because the deck has no alpha and the
    encoder would drop it anyway.
    </remarks>
    """
    size = tile.width
    band_height = max(16, int(size * 0.26))
    overlay = Image.new("RGBA", tile.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([0, size - band_height, size, size], fill=BAND)
    font = images.load_font(_fit_font_size(label, size - 6, int(band_height * 0.7)))
    draw.text((size / 2, size - band_height / 2), label, fill=images.FOREGROUND, font=font, anchor="mm")
    return Image.alpha_composite(tile.convert("RGBA"), overlay).convert("RGB")


def _fit_font_size(text: str, width: int, start: int) -> int:
    """<summary>
    The largest font size at or below ``start`` whose text fits the width.
    </summary>
    <param name="text">The single line to measure.</param>
    <param name="width">The width to fit, in pixels.</param>
    <param name="start">The size to start from and never exceed.</param>
    <returns>A size between 8 and ``start``.</returns>
    <remarks>
    The floor of 8 is real: text that still does not fit at 8 pixels is
    returned at 8 and drawn clipped, on the grounds that unreadably small is
    no better than cut off. Callers get no signal that this happened.
    </remarks>
    """
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    size = start
    while size > 8:
        font = images.load_font(size)
        left, _, right, _ = probe.textbbox((0, 0), text, font=font)
        if right - left <= width:
            break
        size -= 1
    return size


def clock_tile(now: datetime, size: int, background: tuple[int, int, int] = images.BACKGROUND) -> Image.Image:
    """<summary>
    The time as a tile: hours and minutes, centred, 24 hour.
    </summary>
    <param name="now">The moment to show. Passed in rather than read here so
    the tile can be rendered at a fixed time in a test.</param>
    <param name="size">The tile size in pixels. On a strip panel this is the
    visible window, not the square the panel is sent.</param>
    <param name="background">RGB tuple behind the text.</param>
    <returns>An upright tile.</returns>
    <remarks>
    No seconds, and that is a hardware decision rather than a taste one: a
    seconds display means sending an image to the deck every second for as
    long as it is on.
    Whoever draws this owes the redraw that keeps it right, once a minute,
    and <see cref="strip_signature"/> is what tells them the minute has
    turned.
    </remarks>
    """
    tile = images.solid(background, size)
    draw = ImageDraw.Draw(tile)
    draw.text((size / 2, size / 2), now.strftime("%H:%M"), fill=images.FOREGROUND,
              font=images.load_font(int(size * 0.34)), anchor="mm")
    return tile


def date_tile(now: datetime, size: int, background: tuple[int, int, int] = images.BACKGROUND) -> Image.Image:
    """<summary>
    The date as a tile: weekday, day of the month, month, stacked.
    </summary>
    <param name="now">The moment to show, passed in rather than read here.</param>
    <param name="size">The tile size in pixels, the visible window on a
    strip panel.</param>
    <param name="background">RGB tuple behind the text.</param>
    <returns>An upright tile.</returns>
    <remarks>
    Day before month, because the whole project is en-GB, and the three parts
    are drawn separately so the day of the month can be much larger than the
    rest. The weekday and month names come from the machine's locale through
    strftime, so a machine set to another language shows them in it.
    Changes once a day, and the redraw is the caller's job.
    </remarks>
    """
    tile = images.solid(background, size)
    draw = ImageDraw.Draw(tile)
    draw.text((size / 2, size * 0.30), now.strftime("%a"), fill=MUTED,
              font=images.load_font(int(size * 0.20)), anchor="mm")
    draw.text((size / 2, size * 0.56), now.strftime("%d"), fill=images.FOREGROUND,
              font=images.load_font(int(size * 0.36)), anchor="mm")
    draw.text((size / 2, size * 0.82), now.strftime("%b"), fill=MUTED,
              font=images.load_font(int(size * 0.20)), anchor="mm")
    return tile


def weather_tile(observation: Observation | None, size: int, location_name: str = "",
                 background: tuple[int, int, int] = images.BACKGROUND) -> Image.Image:
    """<summary>
    A weather reading as a tile: glyph, temperature, and a short caption.
    </summary>
    <param name="observation">The reading, or None before the first one has
    arrived or after every attempt has failed.</param>
    <param name="size">The tile size in pixels, the visible window on a
    strip panel.</param>
    <param name="location_name">Accepted for the caller's convenience and
    not drawn: the place is chosen once in the settings and there is no room
    for it beside the reading.</param>
    <param name="background">RGB tuple behind everything.</param>
    <returns>An upright tile, always. Never None.</returns>
    <remarks>
    None draws a quiet "no weather" rather than an error or a blank, because
    it is the normal state for the first few seconds after the daemon starts
    and it must not look like a fault.
    The caption is shrunk to fit and never wrapped, so a long label comes out
    small rather than spilling over the edge.
    </remarks>
    """
    tile = images.solid(background, size)
    draw = ImageDraw.Draw(tile)
    if observation is None:
        draw.text((size / 2, size * 0.42), "no", fill=MUTED, font=images.load_font(int(size * 0.2)), anchor="mm")
        draw.text((size / 2, size * 0.62), "weather", fill=MUTED, font=images.load_font(int(size * 0.2)), anchor="mm")
        return tile
    draw_icon(draw, observation.icon, size * 0.5, size * 0.32, size * 0.18)
    draw.text((size / 2, size * 0.68), observation.temperature_text, fill=images.FOREGROUND,
              font=images.load_font(int(size * 0.26)), anchor="mm")
    caption = observation.label
    font = images.load_font(_fit_font_size(caption, size - 6, int(size * 0.14)))
    draw.text((size / 2, size * 0.90), caption, fill=MUTED, font=font, anchor="mm")
    return tile


def draw_icon(draw: ImageDraw.ImageDraw, icon: str, cx: float, cy: float, r: float) -> None:
    """<summary>
    Draw one weather glyph from a few flat shapes.
    </summary>
    <param name="draw">The handle to draw through, already bound to a tile.</param>
    <param name="icon">An icon name as <see cref="weather.describe"/> gives
    it. An unknown name draws a plain cloud rather than nothing.</param>
    <param name="cx">Centre across, in pixels.</param>
    <param name="cy">Centre down, in pixels.</param>
    <param name="r">Roughly the glyph's radius. Everything is drawn as a
    multiple of it, so the glyph scales with the key.</param>
    <remarks>
    Shapes rather than characters so that no emoji or symbol font has to be
    installed, which is the difference between a tile that looks the same on
    both machines and one that shows a box on one of them.
    Draws in place and returns nothing. The glyph is not clipped to the tile,
    so a centre too near an edge spills over it.
    </remarks>
    """
    if icon == "sun":
        _sun(draw, cx, cy, r)
    elif icon == "partly":
        _sun(draw, cx - r * 0.5, cy - r * 0.4, r * 0.7)
        _cloud(draw, cx + r * 0.2, cy + r * 0.3, r * 0.9, CLOUD)
    elif icon == "cloud":
        _cloud(draw, cx, cy, r, CLOUD)
    elif icon == "fog":
        for i in range(3):
            y = cy - r * 0.6 + i * r * 0.6
            draw.line([cx - r, y, cx + r, y], fill=CLOUD, width=max(2, int(r * 0.18)))
    elif icon == "rain":
        _cloud(draw, cx, cy - r * 0.2, r, CLOUD)
        for i in range(3):
            x = cx - r * 0.6 + i * r * 0.6
            draw.line([x, cy + r * 0.5, x - r * 0.2, cy + r * 1.0], fill=RAIN, width=max(2, int(r * 0.15)))
    elif icon == "snow":
        _cloud(draw, cx, cy - r * 0.2, r, CLOUD)
        for i in range(3):
            x = cx - r * 0.6 + i * r * 0.6
            d = r * 0.14
            draw.ellipse([x - d, cy + r * 0.7 - d, x + d, cy + r * 0.7 + d], fill=SNOW)
    elif icon == "storm":
        _cloud(draw, cx, cy - r * 0.2, r, CLOUD)
        bolt = [(cx + r * 0.1, cy + r * 0.3), (cx - r * 0.3, cy + r * 0.9),
                (cx, cy + r * 0.8), (cx - r * 0.1, cy + r * 1.3),
                (cx + r * 0.4, cy + r * 0.6), (cx + r * 0.1, cy + r * 0.7)]
        draw.polygon(bolt, fill=BOLT)
    else:
        _cloud(draw, cx, cy, r, CLOUD)


def _sun(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
    """<summary>
    Draw a sun: eight rays around a filled centre.
    </summary>
    <param name="draw">Target to draw on.</param>
    <param name="cx">Centre x.</param>
    <param name="cy">Centre y.</param>
    <param name="r">Radius out to the tip of the rays, not to the core.</param>
    <remarks>
    ``r`` measures the rays, so the visible disc is a little over half that.
    Sizing a sun by the disc makes it come out much too big. Stroke width has
    a floor of 2 pixels because at key size the proportional width rounds
    down to something invisible.
    </remarks>
    """
    core = r * 0.55
    for i in range(8):
        import math
        angle = i * math.pi / 4
        x1, y1 = cx + math.cos(angle) * r * 0.75, cy + math.sin(angle) * r * 0.75
        x2, y2 = cx + math.cos(angle) * r, cy + math.sin(angle) * r
        draw.line([x1, y1, x2, y2], fill=SUN, width=max(2, int(r * 0.14)))
    draw.ellipse([cx - core, cy - core, cx + core, cy + core], fill=SUN)


def _cloud(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, colour) -> None:
    """
    <summary>
    Draw a cloud glyph: three overlapping discs on a flat base.
    </summary>
    <param name="draw">The ImageDraw.</param>
    <param name="cx">Centre x.</param>
    <param name="cy">Centre y.</param>
    <param name="r">Radius of the whole cloud.</param>
    <param name="colour">Fill colour.</param>
    """
    draw.ellipse([cx - r, cy - r * 0.2, cx, cy + r * 0.5], fill=colour)
    draw.ellipse([cx - r * 0.5, cy - r * 0.6, cx + r * 0.5, cy + r * 0.5], fill=colour)
    draw.ellipse([cx, cy - r * 0.3, cx + r, cy + r * 0.5], fill=colour)
    draw.rectangle([cx - r * 0.5, cy + r * 0.1, cx + r * 0.5, cy + r * 0.5], fill=colour)


def strip_tile(tile: StripTile, now: datetime, observation: Observation | None,
               size: int, location_name: str, visible: tuple[int, int] | None = None,
               default_background: str | None = None) -> Image.Image:
    """<summary>
    The still picture for one strip panel, whatever kind of tile it is
    configured as.
    </summary>
    <param name="tile">The strip tile's config.</param>
    <param name="now">The moment to show on a clock or date tile.</param>
    <param name="observation">The weather reading, or None.</param>
    <param name="size">The square the panel is sent, in pixels.</param>
    <param name="location_name">Passed through to the weather tile, which
    does not draw it.</param>
    <param name="visible">The window the panel actually shows, in pixels, or
    None to treat the whole square as visible.</param>
    <param name="default_background">The page or deck wide background colour.</param>
    <returns>An upright tile of ``size`` square, always.</returns>
    <remarks>
    The panel shows only a central window of ``visible`` pixels out of the
    ``size`` square it is sent, so text tiles are drawn to that window and
    centred, and pictures are fitted to it when the tile says so. Getting
    that wrong is what cuts the ends off a clock: the tile is drawn correctly
    and then loses its edges on the glass.
    A tile of an unknown kind, or an image tile with no picture set, gives a
    plain background rather than raising, so an unfinished config still
    draws.
    This is the still path. Ask <see cref="strip_frames"/> first, as the
    controller does.
    </remarks>
    """
    width, height = visible if visible else (size, size)
    width, height = min(width, size), min(height, size)
    inner = min(width, height)
    bg = strip_background(tile, default_background)
    if tile.kind == "clock":
        return centred(clock_tile(now, inner, bg), size, bg)
    if tile.kind == "date":
        return centred(date_tile(now, inner, bg), size, bg)
    if tile.kind == "weather":
        return centred(weather_tile(observation, inner, location_name, bg), size, bg)
    if tile.kind == "image" and tile.image is not None:
        if tile.fit:
            return centred(picture_box(tile.image, width, height, bg), size, bg)
        return picture_tile(tile.image, size, bg)
    return images.solid(bg, size)


def centred(content: Image.Image, size: int, background: tuple[int, int, int] = images.BACKGROUND) -> Image.Image:
    """<summary>
    Paste a smaller picture into the middle of a background square.
    </summary>
    <param name="content">The picture to centre. Anything larger than
    ``size`` overhangs and is cropped by the paste.</param>
    <param name="size">The side of the square to build, in pixels.</param>
    <param name="background">RGB tuple for the margin round the content.</param>
    <returns>A new square tile.</returns>
    <remarks>
    This is what turns a tile drawn at a strip panel's visible size into the
    full square the panel has to be sent. The margin is never shown on the
    glass, which is why it is filled with the tile's own background rather
    than left black.
    Pasted without a mask, so a picture carrying alpha loses it here. Compose
    before calling.
    </remarks>
    """
    tile = images.solid(background, size)
    tile.paste(content, ((size - content.width) // 2, (size - content.height) // 2))
    return tile


def picture_box(path: Path, width: int, height: int,
                background: tuple[int, int, int] = images.BACKGROUND) -> Image.Image:
    """<summary>
    A picture file scaled to fit a rectangle, aspect kept, centred on the
    background.
    </summary>
    <param name="path">The picture file.</param>
    <param name="width">The box width in pixels.</param>
    <param name="height">The box height in pixels.</param>
    <param name="background">RGB tuple to letterbox against.</param>
    <returns>A picture of exactly the box size, or the missing tile when the
    file cannot be read.</returns>
    <remarks>
    The rectangular counterpart of <see cref="picture_tile"/>, for a strip
    panel whose visible window is not square. The result still has to be
    centred in the full square the panel is sent, which
    <see cref="strip_tile"/> does; used on its own it gives a picture the
    deck cannot take.
    The missing tile comes back square at the smaller side, so it is not the
    box size. That only happens on an unreadable file, and it is centred
    afterwards like anything else.
    </remarks>
    """
    box = Image.new("RGB", (width, height), background)
    try:
        with Image.open(path) as source:
            picture = source.convert("RGBA")
    except (OSError, ValueError):
        return missing_tile(path, min(width, height))
    picture.thumbnail((width, height), Image.Resampling.LANCZOS)
    box.paste(picture, ((width - picture.width) // 2, (height - picture.height) // 2), picture)
    return box


def frame_tile(size: int, inset: int, label: str = "") -> Image.Image:
    """<summary>
    A bright frame drawn ``inset`` pixels in from every edge, with the inset
    printed on it as a big number.
    </summary>
    <param name="size">The tile size in pixels.</param>
    <param name="inset">How far in from each edge to draw the frame.</param>
    <param name="label">Optional text under the number, to say which attempt
    this is.</param>
    <returns>An upright tile.</returns>
    <remarks>
    A calibration aid, not something a user ever sees on a normal page.
    Readable on a small panel where a ruler is not: if the whole frame shows,
    the panel displays at least size minus twice the inset in each direction;
    a missing side says which edge is cut.
    Send a few of these at different insets and the smallest whole frame
    gives the visible window, which is what
    <see cref="strip_tile"/> then wants passing as ``visible``.
    </remarks>
    """
    tile = images.solid((10, 14, 20), size)
    draw = ImageDraw.Draw(tile)
    draw.rectangle([inset, inset, size - 1 - inset, size - 1 - inset], outline=SUN, width=2)
    draw.text((size / 2, size * 0.42), str(inset), fill=images.FOREGROUND,
              font=images.load_font(int(size * 0.42)), anchor="mm")
    if label:
        draw.text((size / 2, size * 0.76), label, fill=MUTED, font=images.load_font(int(size * 0.16)), anchor="mm")
    return tile


def ruler_tile(size: int, label: str) -> Image.Image:
    """<summary>
    A measuring tile: ticks every 5 pixels, numbers every 10, for calibrating
    what a panel actually shows.
    </summary>
    <param name="size">The tile size in pixels.</param>
    <param name="label">Text across the middle, to say which panel this is.</param>
    <returns>An upright tile.</returns>
    <remarks>
    Read the smallest and largest number visible along the top edge and down
    the left edge to learn how much of the picture the panel really shows.
    Ticks are drawn on all four edges so the same tile measures every side,
    and the centre lines show whether the window is centred or offset.
    The numbers are drawn at 7 pixels, which is at the limit of what these
    panels resolve. Every fourth number is picked out in a different colour
    so it can be counted rather than read when a photograph is not sharp
    enough.
    A calibration aid. It is not on any normal page.
    </remarks>
    """
    tile = images.solid((10, 14, 20), size)
    draw = ImageDraw.Draw(tile)
    small = images.load_font(7)
    for x in range(0, size, 5):
        long = x % 10 == 0
        draw.line([x, 0, x, 6 if long else 3], fill=CLOUD)
        draw.line([x, size - 1, x, size - (7 if long else 4)], fill=CLOUD)
        if long:
            draw.text((x + 1, 7), str(x), fill=SUN if x % 20 == 0 else MUTED, font=small)
    for y in range(0, size, 5):
        long = y % 10 == 0
        draw.line([0, y, 6 if long else 3, y], fill=CLOUD)
        draw.line([size - 1, y, size - (7 if long else 4), y], fill=CLOUD)
        if long:
            draw.text((8, y - 3), str(y), fill=SUN if y % 20 == 0 else MUTED, font=small)
    draw.rectangle([0, 0, size - 1, size - 1], outline=RAIN)
    draw.line([size // 2, 0, size // 2, size], fill=(80, 90, 100))
    draw.line([0, size // 2, size, size // 2], fill=(80, 90, 100))
    draw.text((size / 2, size / 2), label, fill=images.FOREGROUND, font=images.load_font(int(size * 0.2)), anchor="mm")
    return tile


def grid_span(size: int, pitch: tuple[int, int] | None) -> tuple[int, int, int, int]:
    """<summary>
    The size of the whole key grid when one picture is laid across it, and
    the pitch used to work that out.
    </summary>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <param name="pitch">(across, down) centre to centre in key pixels, or None
    to treat the keys as touching.</param>
    <returns>(width, height, pitch_x, pitch_y) in the same pixels as ``size``.</returns>
    <remarks>
    The pitch is given back along with the size because every caller needs it
    straight afterwards to work out where a key sits, and this is where the
    None case is resolved. Working it out again at the call site is how the
    two drift apart.
    The grid is the LCD keys only, and its shape comes from the layout
    module rather than from anything here.
    </remarks>
    """
    from . import layout
    pitch_x, pitch_y = pitch if pitch else (size, size)
    return (layout.LCD_COLUMNS - 1) * pitch_x + size, (layout.ROWS - 1) * pitch_y + size, pitch_x, pitch_y


@lru_cache(maxsize=8)
def _wallpaper_canvas(path: str, mtime: float, size: int, pitch: tuple[int, int] | None) -> Image.Image | None:
    """<summary>
    The wallpaper scaled to cover the whole key grid, centred, with the
    aspect ratio kept. None when the file cannot be read.
    </summary>
    <param name="path">The picture file, as a string because a cache key has
    to be hashable and comparable.</param>
    <param name="mtime">The file's modification time. Not used in the body at
    all: it is here purely so that editing the picture changes the cache key
    and the wallpaper is read again. Removing it would leave an edited
    wallpaper showing the old version until the daemon restarts.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <param name="pitch">Key centre to centre spacing, or None for touching
    keys.</param>
    <returns>The cropped canvas, or None.</returns>
    <remarks>
    Cover rather than fit: the picture is scaled until it fills both
    directions and the overflow is cropped off evenly, so there are never
    bars down the side of the deck. A picture of a very different shape from
    the grid therefore loses a lot of itself.
    Cached for eight canvases, which covers a few pages each with its own
    wallpaper. The canvas is held whole and sliced per key by
    <see cref="wallpaper_slice"/>, because slicing is cheap and rescaling is
    not.
    </remarks>
    """
    width, height, _, _ = grid_span(size, pitch)
    try:
        with Image.open(path) as source:
            picture = source.convert("RGB")
    except (OSError, ValueError):
        return None
    scale = max(width / picture.width, height / picture.height)
    scaled = picture.resize((max(1, round(picture.width * scale)), max(1, round(picture.height * scale))),
                            Image.Resampling.LANCZOS)
    left = (scaled.width - width) // 2
    top = (scaled.height - height) // 2
    return scaled.crop((left, top, left + width, top + height))


def wallpaper_slice(path: Path, size: int, pitch: tuple[int, int] | None,
                    row: int, column: int) -> Image.Image | None:
    """<summary>
    One key's square window onto the page's wallpaper.
    </summary>
    <param name="path">The wallpaper file.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <param name="pitch">Key centre to centre spacing in the same pixels, or
    None to treat the keys as touching.</param>
    <param name="row">The key's row, counting from 0 at the top.</param>
    <param name="column">The key's column, counting from 0 at the left.</param>
    <returns>The slice, or None when the picture cannot be read, in which case
    the key draws as if there were no wallpaper.</returns>
    <remarks>
    The picture is scaled to cover the whole key grid at the given pitch and
    this key's ``size`` square is cut out of it at its position, so the slices
    on neighbouring keys line up through the gaps the way the calibration
    pattern does. Cached per picture, size and pitch until the file changes.
    The pitch has to be the deck's real one for that to work. Too small and
    the picture steps inwards at every gap, too large and it steps outwards,
    which is exactly what <see cref="grid_pattern"/> is for measuring.
    Row and column are the grid position, not a device key number. Converting
    between them is the layout module's job.
    </remarks>
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    canvas = _wallpaper_canvas(str(path), mtime, size, pitch)
    if canvas is None:
        return None
    _, _, pitch_x, pitch_y = grid_span(size, pitch)
    left, top = column * pitch_x, row * pitch_y
    return canvas.crop((left, top, left + size, top + size))


def grid_canvas(size: int, pitch_x: int, pitch_y: int) -> Image.Image:
    """<summary>
    One picture spanning the whole key grid at a given pitch, carrying shapes
    that only line up when the pitch is right.
    </summary>
    <param name="size">The key image size in pixels, 95 on the XF-CN001.</param>
    <param name="pitch_x">Centre to centre distance across, in the same pixels.</param>
    <param name="pitch_y">Centre to centre distance down.</param>
    <returns>One picture the size of the whole grid, not a tile.</returns>
    <remarks>
    The canvas is as wide as the five key columns at ``pitch_x`` and as tall as
    the three rows at ``pitch_y``, each key being ``size`` square. It carries
    shapes that only look right when seen through correctly spaced windows:
    both diagonals, a circle round the middle, and lines through the centre.
    When the pitch matches the deck, every line runs straight across the gaps
    between keys; when it is too small the lines step inwards at each gap,
    too large and they step outwards.
    Far too big to send anywhere. It is cut up by
    <see cref="grid_pattern"/>, which is what the deck is actually given.
    </remarks>
    """
    from . import layout
    width = (layout.LCD_COLUMNS - 1) * pitch_x + size
    height = (layout.ROWS - 1) * pitch_y + size
    canvas = Image.new("RGB", (width, height), (10, 14, 20))
    draw = ImageDraw.Draw(canvas)
    thick = max(3, size // 16)
    draw.line([0, 0, width - 1, height - 1], fill=SUN, width=thick)
    draw.line([0, height - 1, width - 1, 0], fill=SUN, width=thick)
    draw.line([width // 2, 0, width // 2, height - 1], fill=CLOUD, width=max(1, thick // 2))
    draw.line([0, height // 2, width - 1, height // 2], fill=CLOUD, width=max(1, thick // 2))
    radius = min(width, height) * 0.42
    cx, cy = width / 2, height / 2
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=RAIN, width=thick)
    draw.rectangle([0, 0, width - 1, height - 1], outline=MUTED, width=max(1, thick // 2))
    return canvas


def grid_pattern(size: int, pitch_x: int, pitch_y: int) -> dict[tuple[int, int], Image.Image]:
    """<summary>
    The calibration canvas cut into one tile per key, keyed by (row, column).
    </summary>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <param name="pitch_x">Centre to centre distance across, in the same
    pixels.</param>
    <param name="pitch_y">Centre to centre distance down.</param>
    <returns>One tile per key position, every one of them ``size`` square.</returns>
    <remarks>
    Each tile is the window the key would show if the picture were laid
    across the deck at that pitch. The middle key also carries the pitch it
    was drawn at, so a photograph of the deck says what was tried and a run
    of photographs can be compared afterwards.
    Only the LCD keys are covered. The positions come from the layout module,
    so a deck with a different grid is handled there and not here.
    </remarks>
    """
    from . import layout
    canvas = grid_canvas(size, pitch_x, pitch_y)
    tiles: dict[tuple[int, int], Image.Image] = {}
    for row in range(layout.ROWS):
        for column in range(layout.LCD_COLUMNS):
            left, top = column * pitch_x, row * pitch_y
            tiles[(row, column)] = canvas.crop((left, top, left + size, top + size))
    middle = (layout.ROWS // 2, layout.LCD_COLUMNS // 2)
    draw = ImageDraw.Draw(tiles[middle])
    draw.text((size / 2, size * 0.86), f"{pitch_x} x {pitch_y}", fill=images.FOREGROUND,
              font=images.load_font(int(size * 0.16)), anchor="mm")
    return tiles


def strip_signature(tile: StripTile, now: datetime, observation: Observation | None) -> tuple:
    """<summary>
    A value that changes whenever the panel's picture would change, and not
    otherwise.
    </summary>
    <param name="tile">The strip tile's config.</param>
    <param name="now">The moment the tile would be drawn at.</param>
    <param name="observation">The weather reading, or None.</param>
    <returns>A hashable tuple to compare against the last one.</returns>
    <remarks>
    This is what stops the deck being sent the same picture several times a
    second. Compare the signature with the one from the last pass and send
    only when it differs.
    The clock is cut to the minute and the date to the day on purpose, so a
    clock panel is redrawn once a minute rather than on every loop. Anything
    finer here would send an image per pass for no visible difference.
    A kind added to the strip tiles must be added here as well. Left out, it
    falls into the last case, which keys on the picture and the animation
    and so never changes: the panel would draw once and then freeze.
    </remarks>
    """
    if tile.kind == "clock":
        return ("clock", now.strftime("%H:%M"), tile.background)
    if tile.kind == "date":
        return ("date", now.strftime("%Y-%m-%d"), tile.background)
    if tile.kind == "weather":
        return ("weather", observation, tile.background)
    return (tile.kind, str(tile.image), tile.fit, tile.animation, tile.background)
