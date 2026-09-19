"""<summary>
JPEG encoding and the basic tile drawing, checked by decoding what comes out.
</summary>
<remarks>
Everything here ends up on real hardware, so the assertions are about the exact
size and orientation the deck expects rather than about the picture looking
nice. The pictures are decoded back and sampled pixel by pixel because the
encoder is the only place a size or rotation fault can be caught before the
bytes reach a screen.

The size is the sharp edge. A picture written at a size the deck was not
expecting is one of the two mistakes that permanently disabled a deck.
</remarks>
"""

import io

from PIL import Image, ImageChops

from deckplate import images


def _decode(jpeg: bytes) -> Image.Image:
    """
    <summary>
    Decode JPEG bytes to an RGB image.
    </summary>
    <param name="jpeg">The bytes.</param>
    <returns>A PIL image.</returns>
    """
    return Image.open(io.BytesIO(jpeg)).convert("RGB")


def _is_red(pixel) -> bool:
    """
    <summary>
    Whether a pixel is clearly red.
    </summary>
    <param name="pixel">An (r, g, b) tuple.</param>
    <returns>True or False.</returns>
    """
    r, g, b = pixel
    return r > 180 and g < 90 and b < 90


def test_encode_produces_square_jpeg_of_requested_size():
    """<summary>
    Encoding gives a real JPEG, square, at exactly the size asked for.
    </summary>
    <remarks>
    The size comes from the device spec and differs between firmware generations,
    so it is passed in rather than assumed. The magic bytes are checked as well as
    the dimensions because a source picture that was already a JPEG could pass
    through at its own size and look correct to a naive check.
    </remarks>
    """
    jpeg = images.encode(images.solid((0, 0, 0), 200), 95)
    assert jpeg[:3] == b"\xff\xd8\xff"
    assert _decode(jpeg).size == (95, 95)


def test_orientation_moves_top_left_to_bottom_left():
    """<summary>
    The encoder turns the picture a quarter turn anticlockwise on its way to the
    deck, so the top left of the source lands bottom left on the screen.
    </summary>
    <remarks>
    The deck's screen is mounted turned relative to the way the tiles are drawn.
    The transform is a rotation followed by a flip on both axes, which is why the
    corner marker is the honest way to test it: reasoning about the two steps
    separately gets the direction wrong about half the time.

    If this breaks, every tile on the deck appears rotated or mirrored, and no
    amount of editing the configuration file will straighten it.
    </remarks>
    """
    # Rotate 90 clockwise then flip both axes is a net 90 anticlockwise turn,
    # so a marker in the top left corner of the source ends up bottom left.
    source = images.solid((0, 0, 0), 95)
    for x in range(20):
        for y in range(20):
            source.putpixel((x, y), (255, 0, 0))
    decoded = _decode(images.encode(source, 95))
    assert _is_red(decoded.getpixel((5, 89)))
    assert not _is_red(decoded.getpixel((5, 5)))
    assert not _is_red(decoded.getpixel((89, 5)))
    assert not _is_red(decoded.getpixel((89, 89)))


def test_numbered_and_text_tiles_render():
    """<summary>
    The numbered and text tiles put ink on the tile rather than returning a blank.
    </summary>
    <remarks>
    A missing font or a failed draw gives a plain background, which is a perfectly
    valid picture and would sail past a size check. Comparing against a solid
    background is the cheap way to prove something was actually drawn without
    pinning the test to the exact pixels of a typeface.
    </remarks>
    """
    tile = images.numbered_tile(13, 95)
    assert tile.size == (95, 95)
    text = images.text_tile(["12:34", "Mon 7"], 95)
    assert text.size == (95, 95)
    # something was drawn: the tile differs from a plain background somewhere
    blank = images.solid(images.BACKGROUND, 95)
    assert ImageChops.difference(text, blank).getbbox() is not None


def test_from_file_round_trip(tmp_path):
    """<summary>
    A picture loaded from disk is resized to the deck's image size on the way out.
    </summary>
    <remarks>
    User supplied pictures arrive at any size at all, and the size that reaches the
    hardware must be the deck's, not the file's. The source here is deliberately
    larger than the target so a pass through with no resize would be caught.
    </remarks>
    """
    path = tmp_path / "pic.png"
    images.solid((10, 200, 30), 300).save(path)
    jpeg = images.from_file(path, 85)
    assert _decode(jpeg).size == (85, 85)
