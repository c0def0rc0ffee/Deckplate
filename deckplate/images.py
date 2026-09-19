"""<summary>
Turn pictures into the JPEG bytes the deck wants on a key, and draw the few
plain tiles the probe and the text panels need.
</summary>
<remarks>
The panels are mounted rotated, so an upright picture has to be rotated 90
degrees clockwise and then flipped on both axes before it is sent. That is the
transform the reference driver uses and it was confirmed on the real deck: the
numbered test tiles came out upright.

That rotation happens in one place only, at the last moment, inside
<see cref="encode"/>. Every other module in the project draws and thinks in
upright pictures, which is the whole reason the drawing code reads normally.
A picture that has already been through <see cref="to_device_orientation"/>
must not be drawn on or fed back in.

Key images are square and their size comes from the device spec, never from a
constant here: 95 pixels on the XF-CN001 and 85 on the CN001. A size hard coded
anywhere works on one deck and quietly draws wrong on the other.
</remarks>
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Quality of the key JPEGs. Paired with subsampling=0 in encode, because the
# panels are tiny and mostly show flat colour with text on it, which is
# exactly what chroma subsampling smears.
JPEG_QUALITY = 90
# The same navy in the two spellings the project needs: a tuple for Pillow and
# a hex string for the configuration page. Change one and change the other.
BACKGROUND = (20, 28, 40)
BACKGROUND_HEX = "#141c28"


def colour(text: str | None, fallback: tuple[int, int, int] = BACKGROUND) -> tuple[int, int, int]:
    """<summary>
    Parse "#rrggbb" or "#rgb" into an RGB tuple, falling back rather than
    raising.
    </summary>
    <param name="text">The colour as written in the config or on the page,
    with or without the leading hash. None and empty text are both allowed
    and both give the fallback.</param>
    <param name="fallback">What to return when the text cannot be read.</param>
    <returns>A tuple of three values from 0 to 255.</returns>
    <remarks>
    Deliberately forgiving. This is fed straight from a config file and a
    colour picker, so a typo should leave one key looking wrong rather than
    stop the daemon halfway through drawing a page. Where a bad colour ought
    to be reported back to the person who typed it, use
    <see cref="animations.parse_colour"/>, which raises instead.
    Alpha is not accepted in any spelling: the deck has no transparency.
    </remarks>
    """
    if not text:
        return fallback
    value = text.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return fallback
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        return fallback
FOREGROUND = (240, 240, 240)
ACCENT = (90, 150, 220)
MARKER = (255, 200, 80)

# Tried in order: two common Linux faces then two Windows ones, so the same
# build draws the same way on either machine.
_FONT_CANDIDATES = ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf")


def load_font(size: int) -> ImageFont.ImageFont:
    """<summary>
    The first installed bold sans face from the candidate list, at the size
    asked for.
    </summary>
    <param name="size">Font size in pixels.</param>
    <returns>A Pillow font. Never None, so callers need no fallback of their
    own.</returns>
    <remarks>
    The last resort is Pillow's built in bitmap face, which ignores the size
    and draws very small. Text that suddenly appears minute on every tile
    means no TrueType face was found rather than a layout fault: install the
    DejaVu fonts and it comes back.
    The name is looked up by Pillow through the system font path, so a face
    present on one machine and absent on the other changes how tiles are
    wrapped as well as how they look, because the wrapping in
    <see cref="tiles.label_lines"/> measures with whatever this returns.
    </remarks>
    """
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def to_device_orientation(image: Image.Image) -> Image.Image:
    """<summary>
    Put an upright picture into the orientation the mounted panels expect.
    </summary>
    <param name="image">An upright picture. It is not modified.</param>
    <returns>A new picture in device orientation.</returns>
    <remarks>
    Rotate 90 clockwise then mirror both axes, whose net effect is 90
    anticlockwise. It is written as those two steps rather than collapsed
    into a single rotate so that it can be compared line by line with the
    reference driver it came from.
    This is not its own inverse: running it twice turns a picture upside
    down. It is called once, from <see cref="encode"/>, and nothing else
    should call it.
    The rotate expands, so a non square picture comes back with its width and
    height swapped. Encode squares the picture first, which is why that never
    shows up in practice.
    </remarks>
    """
    rotated = image.rotate(-90, expand=True)
    return rotated.transpose(Image.Transpose.ROTATE_180)


def encode(image: Image.Image, size: int) -> bytes:
    """<summary>
    Resize a picture to one key, orient it for the panel and encode it as a
    JPEG.
    </summary>
    <param name="image">Any Pillow picture, upright. It is not modified.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <returns>JPEG bytes, ready for <see cref="protocol.image_packets"/>.</returns>
    <remarks>
    The aspect ratio is not kept: the picture is squashed into a square,
    because a key is square and the caller is expected to have fitted the
    picture already. <see cref="tiles.picture_tile"/> is what does that
    fitting, and skipping it is why a picture comes out stretched.
    Converted to RGB first, so an alpha channel is dropped against whatever
    the picture itself carries rather than against the key's background.
    Compose onto a background before calling.
    The result has to fit the 16 bit length field of a BAT header. At these
    sizes it always does, and the protocol module refuses it if it ever
    does not.
    </remarks>
    """
    prepared = image.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)
    prepared = to_device_orientation(prepared)
    out = io.BytesIO()
    prepared.save(out, format="JPEG", quality=JPEG_QUALITY, subsampling=0)
    return out.getvalue()


def from_file(path: str | Path, size: int) -> bytes:
    """<summary>
    Read a picture file from disk and encode it for one key.
    </summary>
    <param name="path">Any file Pillow can open.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <returns>JPEG bytes for that key.</returns>
    <remarks>
    Only the first frame of an animated file is used, and it is squashed to
    the square like any other picture. Animated files belong to the
    animations module, and the fitting belongs to the tiles module: this is
    the short path for a picture that is already the right shape.
    The file handle is closed before the bytes are returned, so nothing here
    keeps a lock on the user's pictures.
    </remarks>
    
    <exception cref="OSError">The file is missing, unreadable, or not a
    picture Pillow understands.</exception>"""
    with Image.open(path) as image:
        return encode(image, size)


def solid(colour: tuple[int, int, int], size: int) -> Image.Image:
    """<summary>
    A new opaque square of one colour: the starting point for every drawn
    tile in the project.
    </summary>
    <param name="colour">An RGB tuple. Inside this function the name hides
    the module level <see cref="colour"/> function, so pass a tuple that has
    already been parsed.</param>
    <param name="size">Side length in pixels.</param>
    <returns>An RGB picture with no alpha channel.</returns>
    <remarks>
    RGB rather than RGBA on purpose. The deck has no transparency, so a tile
    that starts opaque can never reach the encoder carrying an alpha channel
    that would silently be thrown away. Pasting a picture that does have
    alpha onto one of these needs that picture passed as the mask as well,
    which is what the tile drawing code does.
    </remarks>
    """
    return Image.new("RGB", (size, size), colour)


def numbered_tile(number: int, size: int) -> Image.Image:
    """<summary>
    A dark tile with a big number and a small UP marker on the top edge.
    </summary>
    <param name="number">The number to print, normally the device key number
    counting from 1.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <returns>An upright tile, ready for <see cref="encode"/>.</returns>
    <remarks>
    Used by the probe. The marker shows which way the panel is mounted and
    the number shows the firmware's key order, so a photograph of the deck
    with these on it answers both questions at once. If the markers come out
    anywhere but the top edge, the orientation transform is wrong rather
    than the layout.
    Drawn upright like everything else: the rotation is applied later.
    </remarks>
    """
    image = solid(BACKGROUND, size)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, size - 1, size - 1], outline=ACCENT, width=2)
    draw.text((size / 2, size / 2 + 4), str(number), fill=FOREGROUND,
              font=load_font(int(size * 0.55)), anchor="mm")
    draw.text((size / 2, 8), "UP", fill=MARKER, font=load_font(int(size * 0.16)), anchor="mm")
    return image


def text_tile(lines: list[str], size: int, background=BACKGROUND, foreground=FOREGROUND) -> Image.Image:
    """<summary>
    Centred lines of text, each sized to share the height of the key.
    </summary>
    <param name="lines">One string per line, already split. An empty list
    gives a plain background rather than an error.</param>
    <param name="size">The key image size in pixels, from the device spec.</param>
    <param name="background">RGB tuple behind the text.</param>
    <param name="foreground">RGB tuple for the text.</param>
    <returns>An upright tile.</returns>
    <remarks>
    The font is chosen from the line count alone, so a long line runs off
    both edges rather than wrapping or shrinking. That is enough for the
    short readings this draws, and anything the user types goes through
    <see cref="tiles.label_tile"/> instead, which wraps properly.
    </remarks>
    """
    image = solid(background, size)
    draw = ImageDraw.Draw(image)
    if not lines:
        return image
    line_height = size / (len(lines) + 1)
    font = load_font(max(8, int(line_height * 0.8)))
    for index, line in enumerate(lines):
        y = line_height * (index + 1)
        draw.text((size / 2, y), line, fill=foreground, font=font, anchor="mm")
    return image
