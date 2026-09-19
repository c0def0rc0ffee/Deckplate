"""<summary>
Animated tiles: built in effects drawn with Pillow, and animated picture files.
</summary>
<remarks>
Every animation is a list of frames rendered once and played back by the
controller at a fixed rate. Frames are plain upright pictures; the device
orientation and JPEG encoding happen later, once per frame, and are cached.

Built in kinds:
    pulse    a soft glow that breathes in the tile's colour
    spinner  an arc going round
    wave     a ribbon of sine waves drifting across
    rainbow  a gradient cycling through the hues
    scroll   the key's label scrolling across, for names too long to fit

Nothing here knows about the deck. That is deliberate: a loop is built and
held as pictures, so the same frames feed both the hardware and the animated
GIF previews on the configuration page, and the whole module is testable with
no USB anywhere near it.

Frames are built eagerly, the whole loop at once, and kept in an LRU cache.
Rendering is the expensive part and it happens when a page is prepared, never
while a key is being played, so a busy page can stutter as it comes up but
never once it is running.
</remarks>
"""

from __future__ import annotations

import colorsys
import io
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageSequence

from . import images

KINDS = ("pulse", "spinner", "wave", "rainbow", "scroll")
DEFAULT_COLOUR = "#5c9bde"
# The USB link takes well over a thousand frames a second, so smoothness is
# limited by the panels, not the transport. 20 a second looks fluid.
DEFAULT_FPS = 20
MAX_FPS = 30
# How long one loop of a built in effect takes to come round at speed 1.
LOOP_SECONDS = 3.0
# The most frames kept from an animated file. Past this the loop is thinned
# and the rate dropped to match, which bounds both the memory a page holds and
# the work done per frame on the USB link.
MAX_GIF_FRAMES = 150
# Suffixes worth opening to ask whether they are animated. Plain .png is in
# the list because an APNG is usually named .png, and a still PNG simply
# answers no.
ANIMATED_SUFFIXES = (".gif", ".webp", ".apng", ".png")


@dataclass(frozen=True)
class Animation:
    """<summary>
    The settings of one built in animation: which effect, in what colour, how
    fast, and at what frame rate.
    </summary>
    <remarks>
    ``kind`` must be one of <see cref="KINDS"/>; anything else raises when the
    frames are built rather than when the object is made, so a bad config is
    reported against the key being drawn.
    ``speed`` scales the loop length, so a higher speed means a shorter loop
    rather than more frames per second. ``fps`` is what the controller plays
    them back at. The two are independent and both affect how many frames get
    rendered.
    Frozen because it is used as part of the frame cache key, and a mutable
    settings object would hand back another key's frames.
    </remarks>"""

    kind: str
    colour: str = DEFAULT_COLOUR
    speed: float = 1.0
    fps: int = DEFAULT_FPS


def smooth_fps(fps: int) -> int:
    """<summary>
    Read a stored frame rate, lifting the old low default to the current one.
    </summary>
    <param name="fps">The rate as written in the config.</param>
    <returns>The rate to actually play at.</returns>
    <remarks>
    Configs written before the rate was raised still say 8, and there is no
    way to tell a deliberate 8 from an inherited one, so every value at or
    below 8 is treated as the default. That is the trade made knowingly: a
    genuinely slow animation has to be expressed through ``speed``, not
    through a low frame rate.
    Called when a config is read, not when it is written, so nothing rewrites
    the user's file behind their back.
    </remarks>
    """
    return DEFAULT_FPS if fps <= 8 else fps


def parse_colour(text: str) -> tuple[int, int, int]:
    """<summary>
    Parse "#rrggbb" or "#rgb" into an RGB tuple, refusing anything else.
    </summary>
    <param name="text">The colour as written in the config or on the page,
    with or without the leading hash.</param>
    <returns>A tuple of three values from 0 to 255.</returns>
    <remarks>
    The strict counterpart of <see cref="images.colour"/>, which falls back
    silently instead. This one raises because an animation's colour is
    checked when a config is loaded or saved, where a typo can still be shown
    to the person who made it, and the message names the text it was given.
    </remarks>
    
    <exception cref="ValueError">The text is not a colour.</exception>"""
    value = text.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in value):
        raise ValueError(f"'{text}' is not a colour like #5c9bde")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def frame_count(animation: Animation) -> int:
    """<summary>
    How many frames make up one loop of a built in effect.
    </summary>
    <param name="animation">The animation's settings.</param>
    <returns>At least four frames.</returns>
    <remarks>
    The loop lasts LOOP_SECONDS at speed 1, so raising the speed shortens the
    loop and renders fewer frames rather than playing the same frames faster.
    The floor of four keeps a very fast animation from collapsing to a single
    frame, which would look static rather than quick. Speeds below 0.25 are
    clamped, which caps the longest loop and with it the memory a page holds.
    The scroll effect does not use this: it works out its own length from how
    far the text has to travel.
    </remarks>
    """
    return max(4, int(round(animation.fps * LOOP_SECONDS / max(0.25, animation.speed))))


@lru_cache(maxsize=64)
def _cached_frames(kind: str, colour: str, speed: float, fps: int, size: int, label: str,
                   background: tuple[int, int, int]) -> tuple[Image.Image, ...]:
    """<summary>
    Render one whole loop, cached by every parameter that changes what it
    looks like.
    </summary>
    <param name="kind">One of <see cref="KINDS"/>.</param>
    <param name="colour">The effect's colour, as written.</param>
    <param name="speed">Loop speed multiplier.</param>
    <param name="fps">Playback rate, which sets the frame count with speed.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <param name="label">Text for the scroll effect. Ignored by the others,
    but still part of the cache key, so it must be passed consistently.</param>
    <param name="background">RGB tuple behind the effect.</param>
    <returns>The frames of one loop, as a tuple so the cache cannot hand back
    a list a caller has edited.</returns>
    <remarks>
    Every argument is a plain immutable value because an lru_cache key has to
    be hashable: this is why <see cref="frames"/> converts the background to
    a tuple before calling, and why an Animation is unpacked into its fields
    rather than passed whole.
    The cache holds 64 loops, which is comfortably more than a deck full of
    animated keys, so a page redraw normally renders nothing at all. Callers
    get copies from <see cref="frames"/>, never these pictures, because the
    tile code draws labels and the running border straight onto the frames it
    is given.
    </remarks>
    
    <exception cref="ValueError">The kind is not one this module draws, or
    the colour cannot be parsed.</exception>"""
    animation = Animation(kind, colour, speed, fps)
    count = frame_count(animation)
    rgb = parse_colour(colour)
    if kind == "pulse":
        frames = [_pulse(size, rgb, i / count, background) for i in range(count)]
    elif kind == "spinner":
        frames = [_spinner(size, rgb, i / count, background) for i in range(count)]
    elif kind == "wave":
        frames = [_wave(size, rgb, i / count, background) for i in range(count)]
    elif kind == "rainbow":
        frames = [_rainbow(size, i / count) for i in range(count)]
    elif kind == "scroll":
        frames = _scroll(size, rgb, label or "Deckplate", animation, background)
    else:
        raise ValueError(f"unknown animation kind {kind}")
    return tuple(frames)


def frames(animation: Animation, size: int, label: str = "",
           background: tuple[int, int, int] = images.BACKGROUND) -> list[Image.Image]:
    """<summary>
    The frames of one loop of a built in animation, ready to draw on.
    </summary>
    <param name="animation">Which effect, and how it is to look.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <param name="label">Text for the scroll effect. The other kinds ignore
    it, but it still takes part in the cache key.</param>
    <param name="background">RGB tuple behind the effect.</param>
    <returns>Upright frames, one loop, in playback order.</returns>
    <remarks>
    Fresh copies every time. The cached originals are never handed out,
    because the tiles module draws labels and the running border onto these
    frames in place, and a cache holding tiles with somebody else's label on
    them is the bug this prevents.
    Copying is much cheaper than rendering, so calling this on every page
    change is the intended use.
    </remarks>
    
    <exception cref="ValueError">The kind is unknown or the colour is not a
    colour.</exception>"""
    return [f.copy() for f in _cached_frames(animation.kind, animation.colour, animation.speed,
                                             animation.fps, size, label, tuple(background))]


# Effects. Each takes the loop position t in [0, 1).

def _pulse(size: int, rgb, t: float, background) -> Image.Image:
    """<summary>
    A glow that breathes in and out from the centre of the key.
    </summary>
    <param name="size">Key size in pixels, square.</param>
    <param name="rgb">Base colour of the glow.</param>
    <param name="t">Loop position from 0 to 1.</param>
    <param name="background">Colour behind the glow.</param>
    <returns>One frame, in RGB.</returns>
    <remarks>
    Ten rings are drawn from the outside in and composited over the
    background as RGBA, because overlapping translucent ellipses drawn
    straight onto an RGB tile darken where they cross instead of adding.
    The alpha falls off as a power of 1.5 rather than linearly, which is
    what makes the edge read as a glow rather than a flat disc, and the
    floor of 8 keeps the largest ring faintly visible at the bottom of the
    breath. Brightness follows a sine, so the motion eases at both ends.
    </remarks>
    """
    tile = images.solid(background, size)
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    level = 0.5 + 0.5 * math.sin(t * 2 * math.pi)
    centre = size / 2
    rings = 10
    for i in range(rings, 0, -1):
        radius = (size * 0.12) + (size * 0.36) * (i / rings) * (0.7 + 0.3 * level)
        alpha = int(150 * level * (1 - i / rings) ** 1.5) + 8
        draw.ellipse([centre - radius, centre - radius, centre + radius, centre + radius], fill=(*rgb, alpha))
    return Image.alpha_composite(tile.convert("RGBA"), glow).convert("RGB")


def _spinner(size: int, rgb, t: float, background) -> Image.Image:
    """<summary>
    A bright arc travelling once round a dim track, the usual busy
    indicator.
    </summary>
    <param name="size">Key size in pixels, square.</param>
    <param name="rgb">Colour of the moving arc.</param>
    <param name="t">Loop position from 0 to 1, mapped straight to degrees.</param>
    <param name="background">Colour behind the track.</param>
    <returns>One frame, in RGB.</returns>
    <remarks>
    The full circle is drawn first as the unlit track, then the 110 degree
    lit arc over it, so the order here matters. Stroke width has a floor of
    3 pixels: at the deck's key size a width derived purely from the size
    rounds to something that disappears at a glance.
    </remarks>
    """
    tile = images.solid(background, size)
    draw = ImageDraw.Draw(tile)
    margin = size * 0.22
    box = [margin, margin, size - margin, size - margin]
    draw.arc(box, 0, 360, fill=(40, 52, 66), width=max(3, size // 14))
    start = t * 360
    draw.arc(box, start, start + 110, fill=rgb, width=max(3, size // 14))
    return tile


def _wave(size: int, rgb, t: float, background) -> Image.Image:
    """<summary>
    Three sine bands rolling across the key, each dimmer than the last.
    </summary>
    <param name="size">Key size in pixels, square.</param>
    <param name="rgb">Colour of the brightest band.</param>
    <param name="t">Loop position from 0 to 1.</param>
    <param name="background">Colour behind the bands.</param>
    <returns>One frame, in RGB.</returns>
    <remarks>
    Each band is offset in phase as well as in height, which is what stops
    the three reading as one thick line. Points are sampled every two
    pixels rather than every pixel: at this size the difference is not
    visible and it halves the work, which matters because every frame of
    every animated key is built this way.
    </remarks>
    """
    tile = images.solid(background, size)
    draw = ImageDraw.Draw(tile)
    phase = t * 2 * math.pi
    for band in range(3):
        shade = tuple(int(c * (0.45 + 0.275 * band)) for c in rgb)
        offset = band * size * 0.14
        points = []
        for x in range(0, size + 1, 2):
            y = size * 0.5 + offset - size * 0.14 + math.sin(x / size * 2 * math.pi * 1.5 - phase + band) * size * 0.1
            points.append((x, y))
        draw.line(points, fill=shade, width=max(2, size // 20))
    return tile


def _rainbow(size: int, t: float) -> Image.Image:
    """<summary>
    A vertical hue ramp that scrolls, covering half the colour wheel down
    the key.
    </summary>
    <param name="size">Key size in pixels, square.</param>
    <param name="t">Loop position from 0 to 1, added to the hue.</param>
    <returns>One frame, in RGB.</returns>
    <remarks>
    Saturation and value are held below full on purpose: a fully saturated
    ramp is unpleasant at this brightness behind a label. Takes no
    background, unlike the others here, because it fills the key edge to
    edge and nothing would show through.
    </remarks>
    """
    # one row of pixels per line, stretched across: far quicker than per pixel
    column = Image.new("RGB", (1, size))
    for y in range(size):
        hue = (t + y / size * 0.5) % 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 0.75, 0.85)
        column.putpixel((0, y), (int(r * 255), int(g * 255), int(b * 255)))
    return column.resize((size, size), Image.Resampling.NEAREST)


def _scroll(size: int, rgb, text: str, animation: Animation, background) -> list[Image.Image]:
    """<summary>
    The one effect that is not a fixed length loop: the label sliding across,
    for a name too long to fit on a key.
    </summary>
    <remarks>
    The loop length comes from how far the text has to travel rather than
    from <see cref="frame_count"/>, so a long label makes a long loop at the
    same speed. Travel is the text width plus a whole key, which is what
    makes the text leave one edge completely before returning at the other.
    Speed here is pixels a second, held at about 40 at speed 1 whatever the
    frame rate, so raising the rate makes the same movement smoother rather
    than faster.
    Capped at 600 frames: past that a label is long enough that the memory
    matters more than the last few pixels of accuracy in the loop length.
    </remarks>
    """
    font = images.load_font(int(size * 0.34))
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
    width = right - left
    travel = width + size
    # about 40 px a second at speed 1, whatever the frame rate
    step = max(1, int(round(40 * animation.speed / max(1, animation.fps))))
    count = max(4, min(600, travel // step))
    # Start with the text centred, not parked off the right edge: the first
    # frame is what a still preview shows and what the deck shows the instant
    # a page comes up, so it must read. The loop is the same length either way.
    phase = (size + width) // 2
    frames = []
    for i in range(count):
        tile = images.solid(background, size)
        draw = ImageDraw.Draw(tile)
        x = size - (i * step + phase) % travel
        draw.text((x - left, size / 2 - (top + bottom) / 2), text, fill=rgb, font=font)
        frames.append(tile)
    return frames


# Preview encoding

# A preview loop is thinned to this many frames before it is encoded, so a
# long scroll does not become a multi megabyte GIF for the page.
PREVIEW_MAX_FRAMES = 90


def encode_gif(frames: list[Image.Image], fps: int, max_frames: int = PREVIEW_MAX_FRAMES) -> bytes:
    """<summary>
    Encode a loop as an animated GIF for the configuration page.
    </summary>
    <param name="frames">Upright RGB frames, all the same size.</param>
    <param name="fps">The rate the deck plays them at.</param>
    <param name="max_frames">The most frames to keep in the preview.</param>
    <returns>The GIF file bytes, looping forever.</returns>
    <remarks>
    Long loops are thinned to ``max_frames`` and each kept frame is shown for
    correspondingly longer, so the loop still takes the same time to go round
    even though it is visibly less smooth than the deck itself will be.
    Drawing only: nothing here reaches the deck, which never sees a GIF. A
    preview that looks wrong while the key looks right is therefore a fault
    in this function, not in the animation.
    The frame delay is held at 20 milliseconds or more because browsers treat
    anything faster as a request to slow down, and the whole preview would
    then play at the wrong speed.
    </remarks>
    
    <exception cref="ValueError">The list is empty.</exception>"""
    if not frames:
        raise ValueError("no frames to encode")
    keep = 1.0
    if len(frames) > max_frames:
        keep = len(frames) / max_frames
        frames = [frames[int(i * keep)] for i in range(max_frames)]
    duration = max(20, int(round(1000 / max(1, fps) * keep)))
    out = io.BytesIO()
    first, rest = frames[0], frames[1:]
    first.save(out, format="GIF", save_all=True, append_images=rest, duration=duration, loop=0, disposal=1)
    return out.getvalue()


# Animated pictures

def is_animated_file(path: Path) -> bool:
    """<summary>
    Whether a picture file holds more than one frame and should be played
    rather than shown still.
    </summary>
    <param name="path">The file to test.</param>
    <returns>True only for a file that opens and reports several frames.</returns>
    <remarks>
    The suffix is checked first purely to avoid opening every JPEG on a busy
    page; the answer still comes from the file itself, because a still PNG
    and an APNG share a suffix.
    Anything unreadable answers False rather than raising, so a broken file
    falls through to the still picture path and is drawn as a missing tile
    there. This is called while a page is being prepared, so it must never be
    the thing that stops one.
    </remarks>
    """
    if path.suffix.lower() not in ANIMATED_SUFFIXES:
        return False
    try:
        with Image.open(path) as picture:
            return bool(getattr(picture, "is_animated", False)) and getattr(picture, "n_frames", 1) > 1
    except (OSError, ValueError):
        return False


def file_frames(path: Path, size: int, background: tuple[int, int, int] = images.BACKGROUND) -> tuple[list[Image.Image], int]:
    """<summary>
    The frames of an animated picture file, scaled to fit the tile, with the
    rate to play them at.
    </summary>
    <param name="path">An animated file, as
    <see cref="is_animated_file"/> has already confirmed.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <param name="background">RGB tuple to letterbox against, used wherever
    the picture does not fill the square.</param>
    <returns>The upright frames and the frame rate, in that order.</returns>
    <remarks>
    The rate is taken from the file's own frame delays, averaged over the
    whole animation, so a file with varying delays plays at an even
    approximation of itself rather than exactly. Capped at MAX_FPS: a GIF
    claiming hundreds of frames a second would only flood the USB link, and
    the panels cannot show it.
    Long animations are thinned to MAX_GIF_FRAMES by dropping frames evenly
    and the rate is dropped to match, so the animation still takes about the
    same time to go round.
    Aspect ratio is kept and the picture is centred on the background, which
    is the opposite of what <see cref="images.encode"/> does on its own, and
    the reason animated pictures are fitted here rather than left to it.
    The whole file is decoded into memory before anything is returned, so a
    very large animation costs its frame count times the tile size and
    nothing more.
    </remarks>
    
    <exception cref="OSError">The file cannot be read.</exception>
    <exception cref="ValueError">The file is not a picture Pillow
    understands.</exception>"""
    with Image.open(path) as picture:
        total = getattr(picture, "n_frames", 1)
        durations = []
        raw = []
        for frame in ImageSequence.Iterator(picture):
            durations.append(frame.info.get("duration", 100) or 100)
            raw.append(frame.convert("RGBA"))
    average = sum(durations) / max(1, len(durations))
    fps = max(1, min(MAX_FPS, int(round(1000 / max(20, average)))))
    if len(raw) > MAX_GIF_FRAMES:
        keep = len(raw) / MAX_GIF_FRAMES
        fps = max(1, int(round(fps / keep)))
        raw = [raw[int(i * keep)] for i in range(MAX_GIF_FRAMES)]
    out = []
    for frame in raw:
        tile = images.solid(background, size)
        scaled = frame.copy()
        scaled.thumbnail((size, size), Image.Resampling.LANCZOS)
        tile.paste(scaled, ((size - scaled.width) // 2, (size - scaled.height) // 2), scaled)
        out.append(tile)
    return out, fps
