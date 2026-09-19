"""<summary>
Moving key art: the built in effects, animated files read from disk, and the
frames the tile layer hands to the deck.
</summary>
<remarks>
Everything here is checked by looking at pixels, because an animation cannot
be asserted by its type. The recurring shape is a difference against a blank
tile or against the first frame: it proves something actually moved rather
than that a renderer returned the right number of identical pictures, which
is the failure this whole file exists to catch.

Frame counts and rates are not cosmetic. Every frame of every animated key
is a JPEG sent over USB, so a loop that grows without bound costs memory in
the page and bandwidth on the link the key presses have to share.
</remarks>
"""

import io
import pytest
from PIL import Image, ImageChops, ImageSequence

from deckplate import animations, images, tiles
from deckplate.animations import Animation
from deckplate.config import AnimationConfig, KeyConfig, StripTile

SIZE = 95


def test_parse_colour():
    """<summary>
    Pins the colour spellings a saved configuration may contain, and refuses
    the rest.
    </summary>
    <remarks>
    Hash optional, case insensitive, and the three digit short form
    expanded by doubling each digit rather than by padding with zeros, which
    is why "#fff" must come out white and not a very dark grey. Named
    colours are deliberately not supported: accepting some of them and not
    others would be worse than accepting none. A raise is right here because
    this runs while loading the configuration, where the user can be told
    which value is wrong, rather than mid render where it could only be
    guessed at.
    </remarks>
    """
    assert animations.parse_colour("#5c9bde") == (0x5c, 0x9b, 0xde)
    assert animations.parse_colour("5C9BDE") == (0x5c, 0x9b, 0xde)
    assert animations.parse_colour("#fff") == (255, 255, 255)
    for bad in ("#12345", "blue", "#gggggg"):
        try:
            animations.parse_colour(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_every_kind_renders_a_loop_of_distinct_frames():
    """<summary>
    Pins every built in effect, by name from the register, to a loop of
    correctly sized frames in which something actually moves.
    </summary>
    <remarks>
    Iterating the register rather than a written out list is the point: a
    newly added effect is covered the moment it is registered, so a kind the
    configuration page offers can never be one that renders nothing. The
    movement check is a pixel difference against the first frame, because
    the plausible failure is an effect that computes its phase wrongly and
    returns the same picture a dozen times, which looks exactly like a still
    key on the deck and raises nothing.
    </remarks>
    """
    for kind in animations.KINDS:
        frames = animations.frames(Animation(kind), SIZE, label="Hello")
        assert len(frames) >= 4
        assert all(f.size == (SIZE, SIZE) for f in frames)
        # something moves: at least two frames differ
        assert any(ImageChops.difference(frames[0], f).getbbox() for f in frames[1:])


def test_speed_and_fps_change_the_frame_count():
    """<summary>
    Pins the two dials as pulling in the directions a user expects: slower
    means more frames per loop, a higher rate means more frames per second.
    </summary>
    <remarks>
    These are asserted as relations rather than as numbers so the loop
    length can be retuned without rewriting the test, while the sign of each
    relation stays pinned. The one that is easy to invert is speed: it
    multiplies the rate at which the phase advances, so a faster effect
    completes its loop in fewer frames. Getting it backwards gives a
    configuration page where the speed slider works the wrong way round.
    </remarks>
    """
    slow = animations.frame_count(Animation("pulse", speed=0.5, fps=8))
    fast = animations.frame_count(Animation("pulse", speed=2.0, fps=8))
    assert slow > fast
    assert animations.frame_count(Animation("pulse", fps=4)) < animations.frame_count(Animation("pulse", fps=12))


def test_frames_are_cached_and_copies():
    """<summary>
    Pins the render cache handing out copies, so a caller that draws on a
    frame cannot corrupt what everyone else gets.
    </summary>
    <remarks>
    Rendering is expensive enough to cache, and the tile layer routinely
    draws a running mark straight onto the frames it is given. Without the
    copy that mark would be written into the cached loop and would then
    appear on every other key using the same effect, and would never come
    off, since nothing invalidates the cache. Mutating one frame and reading
    the other result is how that sharing is detected.
    </remarks>
    """
    a = animations.frames(Animation("wave"), SIZE)
    b = animations.frames(Animation("wave"), SIZE)
    assert len(a) == len(b)
    a[0].putpixel((0, 0), (255, 0, 0))
    assert b[0].getpixel((0, 0)) != (255, 0, 0)


def _make_gif(path, count=5, duration=80):
    """<summary>
    Write a small animated GIF whose frames are all visibly different.
    </summary>
    <param name="path">Where to write it.</param>
    <param name="count">How many frames.</param>
    <param name="duration">Milliseconds per frame, as stored in the file.</param>
    <remarks>
    The colours are stepped by odd multiples so that no two frames collide
    and the GIF encoder cannot quietly merge them, which would change the
    frame count the reader sees. The frames are 40 pixels square, smaller
    than a key, so the reader is always doing a real resize.
    </remarks>
    """
    frames = [images.solid(((i * 37) % 256, (i * 91) % 256, (i * 53) % 256), 40) for i in range(count)]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=duration, loop=0)


def test_animated_gif_is_detected_and_played(tmp_path):
    """<summary>
    Pins the animated file test on all three answers, and pins the frame
    rate being taken from the file's own timing.
    </summary>
    <remarks>
    The detection has to be cheap and total, because it runs over every key
    image while a page is built. A still image must answer no rather than
    becoming a one frame animation, and a path that does not exist must
    answer no rather than raising, since a configuration can easily name an
    image the user has since moved.

    The rate is read from the stored frame duration and rounded to whole
    frames a second, so 80 milliseconds gives 12 rather than 12.5. Frames
    come back at key size, already resized, because the deck cannot scale.
    </remarks>
    """
    gif = tmp_path / "anim.gif"
    _make_gif(gif)
    still = tmp_path / "still.png"
    images.solid((0, 0, 0), 10).save(still)
    assert animations.is_animated_file(gif)
    assert not animations.is_animated_file(still)
    assert not animations.is_animated_file(tmp_path / "missing.gif")
    frames, fps = animations.file_frames(gif, SIZE)
    assert len(frames) == 5 and fps == 12  # 80 ms frames is 12.5 a second
    assert all(f.size == (SIZE, SIZE) for f in frames)


def test_long_gif_is_thinned(tmp_path):
    """<summary>
    Pins the ceiling on frames kept from a file, and pins the rate staying
    sane after the thinning.
    </summary>
    <remarks>
    A user can drop in a GIF of any length, and without a cap the page would
    hold every frame in memory and the deck would be asked to receive them
    all. Thinning drops frames and lowers the rate together so the loop
    still takes about as long as it did, which is what keeps a thinned
    animation looking like the original rather than like a fast forward.
    The rate must land between one and the maximum: zero would mean a loop
    that never advances, and anything above the maximum would only flood the
    USB link with frames the panels cannot show.
    </remarks>
    """
    gif = tmp_path / "long.gif"
    _make_gif(gif, count=200, duration=50)
    frames, fps = animations.file_frames(gif, SIZE)
    assert len(frames) == animations.MAX_GIF_FRAMES
    assert 1 <= fps <= animations.MAX_FPS


def test_old_configs_get_the_smoother_rate():
    """<summary>
    Pins the one off lift that replaces an old stored default with the
    current one, without touching a rate the user actually chose.
    </summary>
    <remarks>
    Earlier versions wrote a low default rate into every saved animation,
    and those files are still out there. Reading them at the old rate would
    make Deckplate look permanently jerky to anyone upgrading. The boundary
    is what matters: at or below the old default is treated as never having
    been chosen, and anything above is the user's own setting and is passed
    through untouched, so a deliberately slow animation is not sped up
    behind their back.
    </remarks>
    """
    assert animations.smooth_fps(8) == animations.DEFAULT_FPS
    assert animations.smooth_fps(25) == 25


def test_key_frames_for_animation_gif_and_static(tmp_path):
    """<summary>
    Pins the tile layer's decision about which keys animate at all, and the
    running mark drawn on an active one.
    </summary>
    <remarks>
    None is the load bearing answer here. It means this key has nothing that
    moves, so the page never adds it to the animation loop and its picture
    is sent once. A still key that started returning a single frame instead
    would be repainted over USB several times a second forever, for no
    visible change, stealing bandwidth from the key presses. Both a plain
    labelled key and a missing key configuration must give None.

    An animation defined in the configuration runs at the standard rate,
    while one from a file keeps its own frame count. The last pair of lines
    covers the running mark: when a toggle bound to the key is active the
    corner pixel is the marker colour, which is how the deck shows that a
    hold or repeat is still going.
    </remarks>
    """
    gif = tmp_path / "anim.gif"
    _make_gif(gif)
    animated = tiles.key_frames(KeyConfig(0, 0, animation=AnimationConfig("pulse"), label="Go"), SIZE)
    assert animated is not None and animated[1] == animations.DEFAULT_FPS and len(animated[0]) > 4
    from_file = tiles.key_frames(KeyConfig(0, 0, image=gif), SIZE)
    assert from_file is not None and len(from_file[0]) == 5
    assert tiles.key_frames(KeyConfig(0, 0, label="Static"), SIZE) is None
    assert tiles.key_frames(None, SIZE) is None
    marked = tiles.key_frames(KeyConfig(0, 0, animation=AnimationConfig("spinner")), SIZE, active=True)
    assert marked[0][0].getpixel((1, 1)) == tiles.HOLDING


def test_strip_frames_fit_the_visible_window(tmp_path):
    """<summary>
    Pins a strip panel's art being confined to the window the bezel actually
    shows, with the rest left as background.
    </summary>
    <remarks>
    The strip keys are physically smaller than the square tile sent to them,
    so the deck shows only a window in the middle of each tile and the
    surround is hidden behind the case. Art drawn to the full tile would be
    cropped, which in practice means a centred picture losing its edges. The
    two pixel checks are the whole assertion: well outside the window must
    still be background, and inside it must not.

    A clock panel gives None for the same reason a static key does: it is
    repainted on its own schedule rather than driven by the animation loop,
    and adding it there would repaint it several times a second.
    </remarks>
    """
    strip = tiles.strip_frames(StripTile("animation", animation=AnimationConfig("rainbow")), SIZE, (60, 60))
    assert strip is not None
    frames, fps = strip
    assert frames[0].size == (SIZE, SIZE)
    assert frames[0].getpixel((5, 5)) == images.BACKGROUND  # outside the window
    assert frames[0].getpixel((47, 47)) != images.BACKGROUND
    assert tiles.strip_frames(StripTile("clock"), SIZE, (60, 60)) is None
    gif = tmp_path / "anim.gif"
    _make_gif(gif)
    assert tiles.strip_frames(StripTile("image", gif, fit=True), SIZE, (60, 60)) is not None


def test_flash_tile_is_brighter_with_a_border():
    """<summary>
    Pins the pressed look: the tile brightened and given a border, so a
    press is visible however dark the key was.
    </summary>
    <remarks>
    This is the deck's only feedback that a press registered, and it is sent
    before the action runs, so on a key that launches something slow it is
    the only sign anything happened. Brightening alone is not enough, since
    a key that is already pale barely changes, which is why the border is
    drawn in the fixed foreground colour and checked at the corner. The
    input tile here is deliberately dark so the brightening is measurable.
    </remarks>
    """
    tile = images.solid((40, 40, 40), SIZE)
    flashed = tiles.flash_tile(tile)
    assert flashed.getpixel((47, 47))[0] > 40
    assert flashed.getpixel((0, 0)) == images.FOREGROUND


def test_scroll_starts_with_the_text_readable():
    """<summary>
    Frame 0 is what a still preview shows, so the text starts centred, not
    off screen.
    </summary>
    <remarks>
    The natural way to write a scroll is to begin with the text just past
    the right hand edge and walk it left. That looks fine on the deck and is
    useless everywhere a single frame stands in for the key: the
    configuration page preview, and the first paint of a page before the
    animation loop has ticked. Both would show an empty key.

    Two cases are pinned because they differ. A label too long to fit must
    already span the full width at frame zero, and a short one must sit
    centred, with three pixels of tolerance for where the glyphs happen to
    fall.
    </remarks>
    """
    from PIL import ImageChops
    frames = animations.frames(animations.Animation("scroll"), 95, "Request Hangar Access")
    blank = images.solid(images.BACKGROUND, 95)
    box = ImageChops.difference(frames[0], blank).getbbox()
    assert box is not None, "the first scroll frame was empty"
    left, _, right, _ = box
    assert left == 0 and right == 95  # a long label fills the width from the first frame
    short = animations.frames(animations.Animation("scroll"), 95, "Web")
    left, _, right, _ = ImageChops.difference(short[0], blank).getbbox()
    assert abs((left + right) / 2 - 47.5) <= 3  # a short one sits in the middle


def test_encode_gif_thins_long_loops_and_keeps_the_period():
    """<summary>
    Pins the preview encoder: a long loop is thinned to the cap, but the
    thinned GIF still takes as long to come round as the original.
    </summary>
    <remarks>
    This is what the configuration page shows in a browser, so the file has
    to be small enough to send while still looking like the animation the
    deck will play. Thinning without lengthening the remaining frame
    durations would play the loop at several times speed, so the preview and
    the deck would visibly disagree. That total duration check, within a few
    hundred milliseconds of the original period, is the real assertion.

    A loop already under the cap is passed through untouched at its exact
    per frame duration. An empty list raises rather than producing a zero
    frame GIF, because such a file renders as a broken image in the page
    with nothing to explain it.
    </remarks>
    """
    frames = animations.frames(animations.Animation("scroll"), 95, "Request Hangar Access")
    assert len(frames) > animations.PREVIEW_MAX_FRAMES
    data = animations.encode_gif(frames, 20)
    assert data[:6] in (b"GIF89a", b"GIF87a")
    with Image.open(io.BytesIO(data)) as gif:
        assert gif.is_animated and gif.n_frames == animations.PREVIEW_MAX_FRAMES
        assert gif.size == (95, 95)
        # 259 frames at 20 fps is a 12.95 s loop; 90 kept frames must still take that long
        total = sum(frame.info["duration"] for frame in ImageSequence.Iterator(gif))
        assert abs(total - len(frames) / 20 * 1000) < 400
    short = animations.encode_gif(frames[:10], 20)
    with Image.open(io.BytesIO(short)) as gif:
        assert gif.n_frames == 10 and gif.info["duration"] == 50
    with pytest.raises(ValueError):
        animations.encode_gif([], 20)
