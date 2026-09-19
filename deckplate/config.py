"""<summary>
The TOML config file: read it, validate it, hand back plain dataclasses.
</summary>
<remarks>
The file lives in the per user config directory unless a path is given:
Linux   ~/.config/deckplate/config.toml
Windows %APPDATA%\\deckplate\\config.toml

Relative image paths are resolved against the directory the config file is
in, so a config folder with an images/ subfolder next to it moves as a unit.

Everything is validated at load and nothing is validated later. That is the
rule the whole module is built around: by the time a Config exists, every
colour parses, every key combination is spellable, every regular expression
compiles and every page a key jumps to is a page that exists. A typo is then
reported once, with the place in the file it came from, instead of failing
silently on a key press hours later when nobody is watching the log.

Every dataclass here is frozen. Config is read on one thread and used on
several, and a settings object that could be edited in place would let one
of them change what another is halfway through drawing. Reloading builds a
whole new Config rather than mutating the old one.

The document form near the bottom is the same config as plain dicts and
lists, which is what the configuration page edits. It is not a second model:
it round trips through <see cref="document_from_config"/> and
<see cref="to_toml"/>, and the tests hold the two ends together. Writing
TOML needs the small emitter at the end of this file because the standard
library can only read it.
</remarks>
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import themes

APP_NAME = "deckplate"
ROWS = 3
KEY_COLUMNS = 5
STRIP_ROWS = 3
STRIP_TILE_KINDS = ("clock", "date", "weather", "blank")
ACTION_TYPES = ("hotkey", "sequence", "chord", "hold", "boost", "repeat",
                "launch", "url", "page", "brightness", "sleep", "multi")
CHORD_DEFAULTS = {"delay_min_ms": 50, "delay_max_ms": 200}
SEQUENCE_MAX_DELAY_MS = 10_000
SEQUENCE_DEFAULT_DELAY_MS = 100
HOLD_MAX_MS = 600_000
HOLD_DEFAULTS = {"hold_min_ms": 3000, "hold_max_ms": 8000, "release_min_ms": 150, "release_max_ms": 600}
# How long a hotkey may be held down before it is let go. Zero is a plain tap,
# which is what a hotkey has always been.
HOTKEY_MAX_HOLD_MS = 10_000
# Boost: one key held down the whole time while a second cycles on and off.
BOOST_DEFAULTS = {"on_ms": 10_000, "off_ms": 12_000}
# Repeat: tap a key, wait, tap it again, until it is switched off.
REPEAT_DEFAULT_EVERY_MS = 30_000
# The border a key carries while its toggling action runs.
MARK_STYLES = ("auto", "steady", "blink", "none")
MARK_DEFAULT_FLASH_MS = 500
MARK_FLASH_MIN_MS = 100
MARK_FLASH_MAX_MS = 5_000
PAGE_TARGETS = ("next", "previous")
# The second action thresholds, in milliseconds. See deckplate/presses.py for
# what each one means and why a long press fires while the key is still down.
LONG_PRESS_DEFAULT_MS = 400
LONG_PRESS_RANGE = (120, 5000)
DOUBLE_PRESS_DEFAULT_MS = 300
DOUBLE_PRESS_RANGE = (100, 2000)
# How often the focus watcher asks the desktop which window is in front.
FOCUS_POLL_DEFAULT_MS = 200
FOCUS_POLL_RANGE = (50, 5000)
# Key pitch: centre to centre distance between neighbouring keys, in pixels of
# the key's own image, so a 95 px key whose centres are 1.5 keys apart has a
# pitch of 142. Measured on the deck with calibrate-grid; unknown until then.
KEY_PITCH_RANGE = (60, 400)
DEFAULT_CONFIG_RESOURCE = Path(__file__).with_name("default-config.toml")


class ConfigError(ValueError):
    """<summary>
    Something in the config file is wrong, with the place it came from named.
    </summary>
    <remarks>
    The message is written to be read by the person who edited the file, not
    by a developer, so it always names the table or the key it is complaining
    about and never carries a traceback into the interface. Raised for a file
    that cannot be read as well as for one that parses but says something
    impossible, because the user does not care about the difference.
    </remarks>"""

    pass


def config_dir() -> Path:
    """<summary>
    The per user folder the config file and its images live in.
    </summary>
    <returns>A path that may not exist yet. Nothing is created here.</returns>
    <remarks>
    Honours XDG_CONFIG_HOME on Linux and APPDATA on Windows, and falls back
    to the home folder when neither is set, which happens under a service
    account with a stripped environment. Reads the environment on every call
    rather than caching, so the tests can point it somewhere temporary.
    </remarks>
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def default_config_path() -> Path:
    """<summary>
    Where the config file is looked for when no path was given.
    </summary>
    <returns>The config.toml inside <see cref="config_dir"/>, existing or not.</returns>
    """
    return config_dir() / "config.toml"


def ensure_config(path: str | Path | None = None) -> Path:
    """<summary>
    Create the config file from the bundled default if it does not exist yet.
    </summary>
    <param name="path">Where to put it, or None for the default location.</param>
    <returns>The path to a file that now exists.</returns>
    <remarks>
    An existing file is never touched, whatever is in it, so this is safe to
    call on every start and is not a repair or an upgrade step. The images
    folder is created alongside it because relative image paths in the config
    are resolved against that folder, and a first run with nowhere to put a
    picture is a confusing place to begin.
    </remarks>
    
    <exception cref="OSError">The folder or the file could not be created.</exception>"""
    target = Path(path) if path else default_config_path()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAULT_CONFIG_RESOURCE, target)
        (target.parent / "images").mkdir(exist_ok=True)
    return target


@dataclass(frozen=True)
class Action:
    """<summary>
    One thing a key does, as a type name and a bag of parameters.
    </summary>
    <remarks>
    Deliberately not a class per action type. The parameters are checked
    once, at load, by <see cref="_parse_action"/>, and what a type means is
    the actions module's business, so putting the knowledge in both places
    would mean two sets of rules to keep in step.

    Because of that, everything downstream may assume the parameters for this
    type are present and of the right shape, and reach into ``params`` by name
    without checking. The price is that adding a type means adding its
    validation here as well: an unvalidated type would sail through to the
    actions module and fail on a key press instead of at load.

    Frozen, but ``params`` is an ordinary dict and so is not. Nothing edits it
    after parsing, and nothing should start.
    </remarks>"""

    type: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AnimationConfig:
    """<summary>
    A generated animation on a key or a strip tile, rather than a picture.
    </summary>
    <remarks>
    The kind is checked against the animations module at load, so an unknown
    name is refused there and not here. ``fps`` has already been through that
    module's smoothing by the time it reaches this object, so it is what will
    actually be drawn and not what the file asked for. Speed is a multiplier,
    not a duration, and is held between a quarter and four.
    </remarks>"""

    kind: str
    colour: str = "#5c9bde"
    speed: float = 1.0
    fps: int = 20


@dataclass(frozen=True)
class MarkSettings:
    """<summary>
    How a key shows that its toggling action is running.
    </summary>
    <param name="style">"auto" blinks a repeat and holds a steady border for a
    hold or a boost, which is what the two mean. "steady" and "blink" force one
    or the other, and "none" leaves the key alone.</param>
    <param name="colour">The border colour, or None for the built in one.</param>
    <param name="flash_ms">How long each half of a blink lasts.</param>
    <remarks>
    Only the toggling actions have anything to show, so this is ignored on a
    key whose action finishes as soon as it is pressed. A key set to "none"
    still runs its action; it simply gives no sign that it is running, which
    is easy to mistake for the action having failed.

    A short flash_ms costs a redraw and a commit every half cycle, for every
    marked key at once, so the floor is there to stop a page of blinking keys
    filling the USB link with border redraws.
    </remarks>
    """

    style: str = "auto"
    colour: str | None = None
    flash_ms: int = MARK_DEFAULT_FLASH_MS


@dataclass(frozen=True)
class KeyConfig:
    """<summary>
    One key on one page.
    </summary>
    <param name="action">What a normal press does.</param>
    <param name="action_long">What holding the key past the threshold does, if
    anything. Optional, and nothing about a key without one changes.</param>
    <param name="action_double">What two quick presses do, if anything.</param>
    <remarks>
    Row and column count from 0 at the top left, never the firmware's key
    number: the layout module is the only place that converts, and mixing the
    two schemes puts every picture on the wrong key.

    Giving a key a long or a double press action changes the timing of its
    ordinary press, because a press cannot be reported as ordinary until the
    thresholds have passed without the other thing happening. A key with
    neither fires the moment it goes down. That is the presses module's
    doing, but it is set off by what is configured here.

    Every field is optional past the position. A key with no picture, no
    label and no action is legal and draws as background, which is how the
    configuration page saves a key the moment it is created.
    </remarks>
    """

    row: int
    column: int
    image: Path | None = None
    label: str | None = None
    action: Action | None = None
    animation: AnimationConfig | None = None
    background: str | None = None
    action_long: Action | None = None
    action_double: Action | None = None
    mark: MarkSettings = field(default_factory=MarkSettings)

    def actions(self) -> tuple[Action | None, Action | None, Action | None]:
        """<summary>
        The key's three actions together, for walking all of them at once.
        </summary>
        <returns>The plain, long and double actions, in that order.</returns>
        <remarks>
        Any of the three may be None, and usually two of them are. The order
        is fixed and is relied on by the checks that walk every action in a
        config, so do not reorder it.
        </remarks>"""
        return self.action, self.action_long, self.action_double


@dataclass(frozen=True)
class Page:
    """<summary>
    One page of keys.
    </summary>
    <param name="match_window">A regular expression that picks this page when
    the focused window matches it, or None for a page that is only reached by
    hand. Matching is case insensitive and is tried against the window class,
    the process name and the title. Ignored unless [deck] follow_focus is on.
    </param>
    <param name="wallpaper">One picture laid across the whole key grid and cut
    into a slice per key, under every key that has no picture or animation of
    its own. Scaled to cover the grid at the deck's key pitch so it reads as
    continuous through the gaps; without a measured pitch the keys are treated
    as touching.</param>
    <remarks>
    Page names are unique across a config and are how a page action names its
    target, so renaming a page breaks every key that jumps to it. That is
    caught at load rather than on the press.

    ``keys`` is sparse and keyed by (row, column): a position with nothing on
    it is simply absent, not present and empty, so look it up with ``get``
    rather than expecting the full grid. Only the fifteen LCD keys appear
    here; the strip has its own settings because its tiles are a different
    shape and have no press behind them.
    </remarks>
    """

    name: str
    keys: dict[tuple[int, int], KeyConfig]
    match_window: str | None = None
    wallpaper: Path | None = None


@dataclass(frozen=True)
class StripTile:
    """<summary>
    One of the three panels in the display only strip down the right.
    </summary>
    <remarks>
    A strip tile is not a key. It takes a picture but has no button under it
    and never reports a press, so it carries no action and never will.

    ``kind`` is one of the live kinds in STRIP_TILE_KINDS, or "image" or
    "animation", and the last two are set by the parser rather than written
    in the file: a tile given an image path becomes an image tile. The live
    kinds are redrawn on their own clock by the daemon, which is why a clock
    tile has nothing else stored on it.

    ``fit`` shrinks the picture to sit inside the visible area instead of
    filling it. The strip shows less of a picture than a key does, and the
    edges are cut off, so a picture with anything near its border wants this.
    </remarks>"""

    kind: str
    image: Path | None = None
    fit: bool = False
    animation: AnimationConfig | None = None
    background: str | None = None


# The three strip panels show less of the picture than the keys do: the
# panel is narrower than the 95 pixel image and the edges are cut off.
# Measured on Rob's XF-CN001 with `deckplate calibrate-strip` on
# 10 September 2026: a frame 8 px in from the edge just fits, so about 79 px
# of the 95 are shown each way. 78 leaves a little margin.
STRIP_VISIBLE_DEFAULT = (78, 78)


@dataclass(frozen=True)
class StripSettings:
    """<summary>
    The three strip tiles and how much of each panel is actually visible.
    </summary>
    <remarks>
    The visible size is in pixels of the tile's own image, not of the panel,
    and is smaller than the image because the panel crops the edges. It is
    measured per deck with the calibrate-strip command; the default is what
    was measured on the XF-CN001 and is a starting point, not a fact about
    every deck.
    </remarks>"""

    tiles: tuple[StripTile, ...]
    visible_width: int = STRIP_VISIBLE_DEFAULT[0]
    visible_height: int = STRIP_VISIBLE_DEFAULT[1]


OFFICIAL_SOFTWARE_CHOICES = ("ask", "stop", "keep")


DEFAULT_BACKGROUND = "#141c28"


@dataclass(frozen=True)
class DeckSettings:
    """<summary>
    The deck wide settings from [deck].
    </summary>
    <param name="long_press_ms">How long a key must be held before its long
    press action fires.</param>
    <param name="double_press_ms">How long after a release a second press still
    counts as a double press.</param>
    <param name="follow_focus">Whether the deck follows the focused window on to
    the page whose match_window matches it.</param>
    <param name="focus_poll_ms">How often the focused window is checked.</param>
    <param name="key_pitch_x">Centre to centre distance between keys across,
    in pixels of the key image, or None while it has not been measured. With
    key_pitch_y it lets one picture be split over the keys so it reads as
    continuous through the gaps. Found with the calibrate-grid command.</param>
    <param name="key_pitch_y">The same down the columns.</param>
    <remarks>
    These apply to the whole deck and not to one page, so switching pages
    never changes any of them. An action may change the brightness at run
    time, and that change is not written back here: this is what the file
    said, not what the deck is doing now.

    ``sleep_after_minutes`` of 0 means never sleep, not sleep at once. The
    two pitch values are None until somebody runs the calibration, and are
    only meaningful as a pair, which is what <see cref="key_pitch"/> is for.
    </remarks>
    """

    brightness: int = 80
    sleep_after_minutes: int = 0
    official_software: str = "ask"
    press_flash: bool = True
    background: str = DEFAULT_BACKGROUND
    long_press_ms: int = LONG_PRESS_DEFAULT_MS
    double_press_ms: int = DOUBLE_PRESS_DEFAULT_MS
    follow_focus: bool = False
    focus_poll_ms: int = FOCUS_POLL_DEFAULT_MS
    key_pitch_x: int | None = None
    key_pitch_y: int | None = None

    @property
    def key_pitch(self) -> tuple[int, int] | None:
        """<summary>
        The measured key pitch as a pair, or None while it is unknown.
        </summary>
        <returns>(across, down) when both have been measured, else None.</returns>
        <remarks>
        One value on its own is no use, so a config with only one of the two
        answers None here rather than half a pitch. None does not stop a
        wallpaper being drawn: the keys are then treated as touching, which
        looks right on a picture without much detail and wrong on one with a
        line running through it.
        </remarks>"""
        if self.key_pitch_x is None or self.key_pitch_y is None:
            return None
        return self.key_pitch_x, self.key_pitch_y


@dataclass(frozen=True)
class WeatherSettings:
    """<summary>
    Where to fetch the weather for, and how often, for the weather tile.
    </summary>
    <remarks>
    Only used when a strip tile is of the weather kind; a config that has no
    weather tile still carries these and ignores them. The coordinates are a
    plain latitude and longitude, and the location name is a label for the
    tile rather than anything that is looked up.

    Refreshing costs a request to the weather service, so the floor of one
    minute is there to keep a mistyped value from hammering it.
    </remarks>"""

    latitude: float = 51.5072
    longitude: float = -0.1276
    units: str = "metric"
    refresh_minutes: int = 10
    location_name: str = "London"


LOOPBACK_ADDRESSES = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True)
class ServerSettings:
    """<summary>
    The local HTTP server that serves the configuration page.
    </summary>
    <remarks>
    Bound to the loopback address by default, which is the safe setting: the
    page can change what every key does and can launch a command, so anything
    that can reach it can run programs as this user.

    Binding anywhere else is allowed but demands a token, and that is checked
    at load rather than at startup so the refusal is reported with the rest of
    the config errors. The token is a shared secret and belongs in a config
    file kept out of version control, never in the repository.
    </remarks>"""

    enabled: bool = True
    bind: str = "127.0.0.1"
    port: int = 8765
    token: str | None = None


@dataclass(frozen=True)
class Config:
    """<summary>
    A whole validated config file: everything the daemon needs to run.
    </summary>
    <remarks>
    Existing at all is the guarantee. Nothing downstream revalidates, so
    anything that builds one of these by hand rather than through
    <see cref="parse"/> owes the same checks, and the tests that build one
    directly are the only place that is reasonable.

    ``base_dir`` is what relative image paths were resolved against and is
    kept so the document form can turn absolute paths back into relative
    ones. ``path`` is None for a config parsed from text rather than read
    from a file, which is the usual case in the tests and when the
    configuration page previews an edit before saving it.
    </remarks>"""

    deck: DeckSettings
    weather: WeatherSettings
    strip: tuple[StripTile, ...]
    pages: tuple[Page, ...]
    base_dir: Path
    path: Path | None = None
    server: ServerSettings = ServerSettings()
    strip_visible: tuple[int, int] = STRIP_VISIBLE_DEFAULT

    def page_named(self, name: str) -> Page | None:
        """<summary>
        The page with this exact name, or None when there is no such page.
        </summary>
        <param name="name">A page name, matched exactly and case sensitively.</param>
        <returns>The page, or None.</returns>
        <remarks>
        None should not happen for a name that came out of a page action,
        since those are checked at load, so treat it as a bug rather than as
        a user error. It does happen for a name typed somewhere later, such
        as on the command line.
        </remarks>
        """
        for page in self.pages:
            if page.name == name:
                return page
        return None


def load(path: str | Path) -> Config:
    """<summary>
    Read a config file from disk and validate the whole of it.
    </summary>
    <param name="path">The config.toml to read.</param>
    <returns>A validated Config, with base_dir set to the file's folder.</returns>
    <remarks>
    Relative image paths end up resolved against the folder the file is in,
    not against the working directory, so the same config works whatever the
    daemon was started from. Does not create the file: call
    <see cref="ensure_config"/> first if it may not be there yet.
    </remarks>
    
    <exception cref="ConfigError">The file cannot be read, is not valid TOML,
    or says something that is not allowed.</exception>"""
    path = Path(path)
    try:
        # utf-8-sig: Notepad and PowerShell like to put a byte order mark at
        # the front of a file, and TOML would reject it as an invalid statement.
        text = path.read_text(encoding="utf-8-sig")
    except OSError as err:
        raise ConfigError(f"cannot read {path}: {err}") from err
    return parse(text, path.parent, path)


def parse(text: str, base_dir: str | Path, path: Path | None = None) -> Config:
    """<summary>
    Validate config text and build the Config, without touching the disk.
    </summary>
    <param name="text">The whole file as text, byte order mark already gone.</param>
    <param name="base_dir">The folder relative image paths resolve against.</param>
    <param name="path">Where the text came from, or None when it came from
    the configuration page rather than from a file.</param>
    <returns>A validated Config.</returns>
    <remarks>
    This is where the promise that a Config is valid is actually kept, and it
    is the only way to make one that anything else should use. The
    configuration page parses an edit through here before saving it, so a bad
    edit is refused while the good file is still on disk.

    Errors are raised one at a time and parsing stops at the first, so a file
    with several faults takes several attempts to fix. That is deliberate:
    one clear message about one place beats a list to work through.

    The order the sections are parsed in matters only for pages, which are
    done last because the check that every page action names a real page
    needs every page name to be known first.
    </remarks>
    
    <exception cref="ConfigError">The text is not valid TOML, or says
    something that is not allowed.</exception>"""
    base_dir = Path(base_dir)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"not valid TOML: {err}") from err

    deck = _parse_deck(_table(data, "deck", {}))
    weather = _parse_weather(_table(data, "weather", {}))
    strip_table = _table(data, "strip", {})
    strip = _parse_strip(strip_table, base_dir)
    visible = (_int(strip_table, "visible_width", "[strip]", STRIP_VISIBLE_DEFAULT[0], 8, 200),
               _int(strip_table, "visible_height", "[strip]", STRIP_VISIBLE_DEFAULT[1], 8, 200))
    pages = _parse_pages(data.get("pages"), base_dir)
    server = _parse_server(_table(data, "server", {}))
    return Config(deck=deck, weather=weather, strip=strip, pages=pages, base_dir=base_dir,
                  path=path, server=server, strip_visible=visible)


# Parsing helpers. Every error names the place in the file it came from.


def _table(data: dict, key: str, default: Any) -> dict:
    """
    <summary>
    A sub table of a parsed config, or the default when the key is absent.
    </summary>
    <param name="data">The parsed document or an enclosing table.</param>
    <param name="key">Table name.</param>
    <param name="default">Returned when the key is missing.</param>
    <returns>A dict.</returns>
    <exception cref="ConfigError">When the key is present but is not a table.</exception>
    """
    value = data.get(key, default)
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _int(table: dict, key: str, where: str, default: int | None = None,
         low: int | None = None, high: int | None = None) -> int:
    """<summary>
    A whole number from a table, checked for type and range.
    </summary>
    <param name="table">The TOML table to read from.</param>
    <param name="key">The key inside it.</param>
    <param name="where">How to name this place in an error, such as "[deck]".</param>
    <param name="default">Used when the key is absent. None makes it required.</param>
    <param name="low">Lowest allowed value, or None for no floor.</param>
    <param name="high">Highest allowed value, or None for no ceiling.</param>
    <returns>The value, known to be an int inside the range.</returns>
    <remarks>
    A bool is rejected even though Python counts one as an int, because
    ``true`` in a TOML file means a mistake here and would otherwise be
    accepted as 1 and quietly do something.

    A default of None is the only way to say required, so there is no way to
    have an optional value that defaults to nothing. That is why the callers
    that want an absent value to stay absent test for the key themselves.
    </remarks>
    
    <exception cref="ConfigError">Absent and required, the wrong type, or out
    of range.</exception>"""
    value = table.get(key, default)
    if value is None:
        raise ConfigError(f"{where}: '{key}' is required")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: '{key}' must be a whole number")
    if low is not None and value < low or high is not None and value > high:
        raise ConfigError(f"{where}: '{key}' must be between {low} and {high}")
    return value


def _float(table: dict, key: str, where: str, default: float) -> float:
    """<summary>
    A number from a table, accepting a whole number and widening it.
    </summary>
    <param name="table">The TOML table to read from.</param>
    <param name="key">The key inside it.</param>
    <param name="where">How to name this place in an error.</param>
    <param name="default">Used when the key is absent. Always has one, so
    nothing read through here can be required.</param>
    <returns>The value as a float.</returns>
    <remarks>
    An int is accepted and widened, because writing a latitude as ``51`` in
    the file is reasonable and TOML would hand it back as an int. A bool is
    rejected for the same reason as in <see cref="_int"/>. No range checking:
    the callers that need one do it themselves against their own limits.
    </remarks>
    
    <exception cref="ConfigError">The value is not a number.</exception>"""
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: '{key}' must be a number")
    return float(value)


def _str(table: dict, key: str, where: str, default: str | None = None, required: bool = False) -> str | None:
    """<summary>
    Text from a table, or None when it is absent and not required.
    </summary>
    <param name="table">The TOML table to read from.</param>
    <param name="key">The key inside it.</param>
    <param name="where">How to name this place in an error.</param>
    <param name="default">Used when the key is absent.</param>
    <param name="required">True to refuse an absent value rather than
    returning None.</param>
    <returns>The text as written, spacing included, or None.</returns>
    <remarks>
    Text that is present but only whitespace is refused rather than returned,
    since an empty name or an empty command is a mistake every time. That
    means None and "" are not interchangeable here: None is absent, and ""
    never comes back.

    The return type includes None even with ``required`` set, because the type
    checker cannot see the connection, which is why so many callers end with
    ``or`` and a fallback that can never be reached.
    </remarks>
    
    <exception cref="ConfigError">Absent and required, not text, or blank.</exception>"""
    value = table.get(key, default)
    if value is None:
        if required:
            raise ConfigError(f"{where}: '{key}' is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: '{key}' must be text")
    return value


def _parse_deck(table: dict) -> DeckSettings:
    """<summary>
    Build the deck wide settings from the [deck] table.
    </summary>
    <param name="table">The [deck] table, or an empty dict when it is absent.</param>
    <returns>Validated DeckSettings, every field defaulted where missing.</returns>
    <remarks>
    An absent [deck] table is legal and gives a working deck on defaults, so
    this must cope with an empty dict.

    The two key pitches are the exception to defaulting: they are read only
    when the key is actually present, because None means not yet measured and
    no number could stand in for that.
    </remarks>
    
    <exception cref="ConfigError">A value is of the wrong type, out of range,
    or not one of the allowed choices.</exception>"""
    official = _str(table, "official_software", "[deck]", "ask") or "ask"
    if official not in OFFICIAL_SOFTWARE_CHOICES:
        raise ConfigError(f"[deck]: 'official_software' must be one of {', '.join(OFFICIAL_SOFTWARE_CHOICES)}")
    flash = table.get("press_flash", True)
    if not isinstance(flash, bool):
        raise ConfigError("[deck]: 'press_flash' must be true or false")
    follow = table.get("follow_focus", False)
    if not isinstance(follow, bool):
        raise ConfigError("[deck]: 'follow_focus' must be true or false")
    return DeckSettings(
        brightness=_int(table, "brightness", "[deck]", 80, 0, 100),
        sleep_after_minutes=_int(table, "sleep_after_minutes", "[deck]", 0, 0, 24 * 60),
        official_software=official,
        press_flash=flash,
        background=_colour(table, "background", "[deck]", DEFAULT_BACKGROUND) or DEFAULT_BACKGROUND,
        long_press_ms=_int(table, "long_press_ms", "[deck]", LONG_PRESS_DEFAULT_MS, *LONG_PRESS_RANGE),
        double_press_ms=_int(table, "double_press_ms", "[deck]", DOUBLE_PRESS_DEFAULT_MS, *DOUBLE_PRESS_RANGE),
        follow_focus=follow,
        focus_poll_ms=_int(table, "focus_poll_ms", "[deck]", FOCUS_POLL_DEFAULT_MS, *FOCUS_POLL_RANGE),
        key_pitch_x=_int(table, "key_pitch_x", "[deck]", 0, *KEY_PITCH_RANGE) if "key_pitch_x" in table else None,
        key_pitch_y=_int(table, "key_pitch_y", "[deck]", 0, *KEY_PITCH_RANGE) if "key_pitch_y" in table else None,
    )


def _colour(table: dict, key: str, where: str, default: str | None = None) -> str | None:
    """<summary>
    A '#rrggbb' colour, checked now so a typo is reported at load.
    </summary>
    <param name="table">The table to read from.</param>
    <param name="key">The key holding the colour.</param>
    <param name="where">How to name this place in an error.</param>
    <param name="default">Used when the key is absent, or None for no colour.</param>
    <returns>The colour normalised to lower case with a leading hash, or None.</returns>
    <remarks>
    The leading hash is optional in the file and always present in the
    answer, so everything downstream can assume one form. Checking here is
    what turns a mistyped colour into one error at load rather than a key
    that silently refuses to draw.

    The animations import is deliberately inside the function: config is
    imported by almost everything, and importing the drawing code at module
    scope would drag Pillow into tools that never draw anything.
    </remarks>
    
    <exception cref="ConfigError">The value is not text, or is not a colour
    the animations module can read.</exception>"""
    from . import animations

    value = _str(table, key, where, default)
    if value is None:
        return None
    try:
        animations.parse_colour(value)
    except ValueError as err:
        raise ConfigError(f"{where}: '{key}': {err}") from err
    return value.strip().lower() if value.startswith("#") else "#" + value.strip().lower()


def _parse_mark(raw: Any, where: str) -> MarkSettings:
    """<summary>
    The key's running border settings, defaulted when the table is absent.
    </summary>
    <param name="raw">The key's ``mark`` value, or None when there is none.</param>
    <param name="where">How to name this key in an error.</param>
    <returns>MarkSettings, all defaults when nothing was written.</returns>
    <remarks>
    Absent and all defaults are the same thing, on purpose, which is what
    lets <see cref="_mark_document"/> leave the table out again when writing
    the file back rather than filling it with values nobody typed.
    </remarks>
    
    <exception cref="ConfigError">``mark`` is not a table, or its style is not
    one of MARK_STYLES.</exception>"""
    if raw is None:
        return MarkSettings()
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: 'mark' must be a table")
    style = (_str(raw, "style", where, "auto") or "auto").strip().lower()
    if style not in MARK_STYLES:
        raise ConfigError(f"{where}: 'mark.style' must be one of {', '.join(MARK_STYLES)}")
    return MarkSettings(
        style=style,
        colour=_colour(raw, "colour", where),
        flash_ms=_int(raw, "flash_ms", where, MARK_DEFAULT_FLASH_MS,
                      MARK_FLASH_MIN_MS, MARK_FLASH_MAX_MS),
    )


def _mark_document(mark: MarkSettings | None) -> dict | None:
    """<summary>
    The document form, or None when it is all defaults so nothing is written.
    </summary>
    <param name="mark">The key's mark settings, or None.</param>
    <returns>A plain dict, or None to leave the table out of the file.</returns>
    <remarks>
    The None for an all defaults mark is what keeps a saved config as short
    as the one the user wrote. Without it, every key in the file would grow a
    mark table the moment the configuration page saved once, and the round
    trip test comparing a config with its rewritten self would still pass
    while the file quietly filled with noise.
    </remarks>
    """
    if mark is None or mark == MarkSettings():
        return None
    return {"style": mark.style, "colour": mark.colour, "flash_ms": mark.flash_ms}


def _parse_animation(table: Any, where: str) -> AnimationConfig:
    """<summary>
    Build an animation from either a table or a bare kind name.
    </summary>
    <param name="table">An animation table, or just the kind as a string.</param>
    <param name="where">How to name this place in an error.</param>
    <returns>A validated AnimationConfig with the frame rate already smoothed.</returns>
    <remarks>
    A plain string is accepted as shorthand for a table with only a kind,
    because most animations want nothing but their name.

    The frame rate that comes back is not the one asked for. It goes through
    the animations module's smoothing first, which picks a rate that divides
    evenly into the loop, so a config asking for something awkward gets the
    nearest workable rate rather than a stuttering key.
    </remarks>
    
    <exception cref="ConfigError">Not a table or a name, an unknown kind, a
    bad colour, or a speed or frame rate out of range.</exception>"""
    from . import animations

    if isinstance(table, str):
        table = {"kind": table}
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: 'animation' must be a table or a kind name")
    kind = _str(table, "kind", where, required=True) or ""
    if kind not in animations.KINDS:
        raise ConfigError(f"{where}: unknown animation '{kind}', expected one of {', '.join(animations.KINDS)}")
    colour = _str(table, "colour", where, animations.DEFAULT_COLOUR) or animations.DEFAULT_COLOUR
    try:
        animations.parse_colour(colour)
    except ValueError as err:
        raise ConfigError(f"{where}: {err}") from err
    speed = _float(table, "speed", where, 1.0)
    if not 0.25 <= speed <= 4:
        raise ConfigError(f"{where}: 'speed' must be between 0.25 and 4")
    fps = _int(table, "fps", where, animations.DEFAULT_FPS, 1, animations.MAX_FPS)
    return AnimationConfig(kind=kind, colour=colour, speed=speed, fps=animations.smooth_fps(fps))


def _parse_server(table: dict) -> ServerSettings:
    """<summary>
    Build the configuration page server settings from the [server] table.
    </summary>
    <param name="table">The [server] table, or an empty dict when absent.</param>
    <returns>Validated ServerSettings.</returns>
    <remarks>
    The refusal to bind off the local machine without a token is the one
    check here that is about safety rather than about types, and it is done
    at load so the daemon cannot come up listening to the network on a config
    that merely forgot the token. Do not relax it to a warning.

    Port 0 is allowed and means let the operating system choose, which is how
    the tests start a server without picking a port that might be taken.
    </remarks>
    
    <exception cref="ConfigError">A value is of the wrong type, the port is
    out of range, or a non local bind address was given with no token.</exception>"""
    enabled = table.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("[server]: 'enabled' must be true or false")
    bind = _str(table, "bind", "[server]", "127.0.0.1") or "127.0.0.1"
    token = _str(table, "token", "[server]")
    if bind not in LOOPBACK_ADDRESSES and not token:
        raise ConfigError("[server]: a 'token' is required when 'bind' is not the local machine")
    return ServerSettings(
        enabled=enabled, bind=bind,
        port=_int(table, "port", "[server]", 8765, 0, 65535),
        token=token,
    )


def _parse_weather(table: dict) -> WeatherSettings:
    """<summary>
    Build the weather tile settings from the [weather] table.
    </summary>
    <param name="table">The [weather] table, or an empty dict when absent.</param>
    <returns>Validated WeatherSettings.</returns>
    <remarks>
    Parsed whether or not the config has a weather tile, so an absent table
    must give working defaults rather than an error. The coordinates are not
    range checked: an impossible one gets no weather back from the service
    and shows as an empty tile, which is a clearer answer than refusing to
    load the whole config over a tile nobody may be using.
    </remarks>
    
    <exception cref="ConfigError">A value is of the wrong type, or the units
    are neither metric nor imperial.</exception>"""
    units = _str(table, "units", "[weather]", "metric")
    if units not in ("metric", "imperial"):
        raise ConfigError("[weather]: 'units' must be 'metric' or 'imperial'")
    return WeatherSettings(
        latitude=_float(table, "latitude", "[weather]", 51.5072),
        longitude=_float(table, "longitude", "[weather]", -0.1276),
        units=units,
        refresh_minutes=_int(table, "refresh_minutes", "[weather]", 10, 1, 24 * 60),
        location_name=_str(table, "location_name", "[weather]", "London") or "London",
    )


def _parse_strip(table: dict, base_dir: Path) -> tuple[StripTile, ...]:
    """<summary>
    Build the three strip tiles from the [strip] table.
    </summary>
    <param name="table">The [strip] table, or an empty dict when absent.</param>
    <param name="base_dir">The folder relative image paths resolve against.</param>
    <returns>Exactly STRIP_ROWS tiles, in top to bottom order.</returns>
    <remarks>
    Exactly three, never fewer: the strip has three panels and a short list is
    refused rather than padded, because a config that means to leave one empty
    should say "blank" and be readable as meaning it.

    A bare string is a kind if it is one of STRIP_TILE_KINDS and an image path
    otherwise, which is the trap here. A kind name misspelt is not an error:
    it becomes an image path that does not exist, and shows up as a tile with
    the missing marker rather than as a message at load.
    </remarks>
    
    <exception cref="ConfigError">The list is not exactly three entries, or an
    entry is neither a known kind, an image path, nor a table this
    understands.</exception>"""
    raw = table.get("tiles", ["clock", "date", "weather"])
    if not isinstance(raw, list) or len(raw) != STRIP_ROWS:
        raise ConfigError(f"[strip]: 'tiles' must be a list of exactly {STRIP_ROWS} entries")
    tiles = []
    for index, entry in enumerate(raw):
        where = f"[strip] tiles[{index}]"
        if isinstance(entry, str):
            if entry in STRIP_TILE_KINDS:
                tiles.append(StripTile(kind=entry))
            else:
                tiles.append(StripTile(kind="image", image=_resolve(entry, base_dir)))
        elif isinstance(entry, dict) and isinstance(entry.get("image"), str):
            fit = entry.get("fit", False)
            if not isinstance(fit, bool):
                raise ConfigError(f"{where}: 'fit' must be true or false")
            tiles.append(StripTile(kind="image", image=_resolve(entry["image"], base_dir), fit=fit,
                                   background=_colour(entry, "background", where)))
        elif isinstance(entry, dict) and entry.get("animation") is not None:
            tiles.append(StripTile(kind="animation", animation=_parse_animation(entry["animation"], where),
                                   background=_colour(entry, "background", where)))
        elif isinstance(entry, dict) and entry.get("image") is None and isinstance(entry.get("kind"), str):
            kind = entry["kind"]
            if kind not in STRIP_TILE_KINDS:
                raise ConfigError(f"{where}: unknown kind '{kind}'")
            tiles.append(StripTile(kind=kind, background=_colour(entry, "background", where)))
        else:
            raise ConfigError(f"{where}: must be one of {', '.join(STRIP_TILE_KINDS)} or an image path")
    return tuple(tiles)


def _parse_pages(raw: Any, base_dir: Path) -> tuple[Page, ...]:
    """<summary>
    Build every page, then check that page actions point at pages that exist.
    </summary>
    <param name="raw">The [[pages]] list from the file.</param>
    <param name="base_dir">The folder relative image paths resolve against.</param>
    <returns>The pages in file order. The first is the one shown at startup.</returns>
    <remarks>
    Two passes, and they cannot be merged. The first builds the pages and
    collects the names; only then can the second check that every page action
    has somewhere to go, because a page is allowed to jump forwards to one
    defined later in the file.

    The check reaches into multi actions as well, so a page jump buried in a
    step is caught like any other. The words in PAGE_TARGETS are allowed
    anywhere a page name is, and mean next and previous rather than a page
    somebody forgot to define.

    Order is kept, which matters: the first page is what the deck shows when
    it starts, and next and previous walk the list as written.
    </remarks>
    
    <exception cref="ConfigError">There are no pages, a page is not a table, a
    name is used twice, or a page action names a page that does not exist.</exception>"""
    if not isinstance(raw, list) or not raw:
        raise ConfigError("at least one [[pages]] entry is required")
    pages: list[Page] = []
    names: set[str] = set()
    for index, table in enumerate(raw):
        where = f"[[pages]] #{index + 1}"
        if not isinstance(table, dict):
            raise ConfigError(f"{where}: must be a table")
        name = _str(table, "name", where, required=True) or ""
        if name in names:
            raise ConfigError(f"{where}: page name '{name}' is used twice")
        names.add(name)
        wallpaper = _str(table, "wallpaper", where)
        pages.append(Page(name=name, keys=_parse_keys(table.get("keys", []), f"page '{name}'", base_dir),
                          match_window=_pattern(table, "match_window", where),
                          wallpaper=_resolve(wallpaper, base_dir) if wallpaper else None))

    for page in pages:
        for key in page.keys.values():
            where = f"page '{page.name}' key {key.row},{key.column}"
            for action in (key.action, key.action_long, key.action_double):
                _check_page_targets(action, names, where)
    return tuple(pages)


def _pattern(table: dict, key: str, where: str) -> str | None:
    """<summary>
    A regular expression, compiled now so a typo is reported at load.
    </summary>
    <param name="table">The table to read from.</param>
    <param name="key">The key holding the expression.</param>
    <param name="where">How to name this place in an error.</param>
    <returns>The expression as written, or None when the key is absent.</returns>
    <remarks>
    The compiled object is thrown away and the text is what is kept, because
    the caller that matches windows compiles it again with its own flags.
    Compiling here is purely the check, and it uses the same case insensitive
    flag the matcher does so that a pattern which compiles at load cannot
    fail later.
    </remarks>
    
    <exception cref="ConfigError">The value is not text, or will not
    compile.</exception>"""
    value = _str(table, key, where)
    if value is None:
        return None
    try:
        re.compile(value, re.IGNORECASE)
    except re.error as err:
        raise ConfigError(f"{where}: '{key}' is not a valid regular expression: {err}") from err
    return value


def _parse_keys(raw: Any, where: str, base_dir: Path) -> dict[tuple[int, int], KeyConfig]:
    """<summary>
    Build one page's keys, keyed by their (row, column) position.
    </summary>
    <param name="raw">The page's ``keys`` list, possibly absent.</param>
    <param name="where">How to name this page in an error.</param>
    <param name="base_dir">The folder relative image paths resolve against.</param>
    <returns>A sparse dict: only positions that were written appear.</returns>
    <remarks>
    Only the fifteen LCD keys can be configured here, so the column is held
    below KEY_COLUMNS and the strip column is out of reach. A strip tile is
    configured in [strip] instead, and an action on one would never fire
    anyway.

    The same position twice is refused rather than the later one winning,
    because silently dropping a key somebody wrote is worse than making them
    choose which one they meant.

    A page with no keys at all is allowed and draws as an empty grid. That is
    what a page looks like the moment the configuration page creates it.
    </remarks>
    
    <exception cref="ConfigError">An entry is not a table, a position is off
    the key grid or used twice, or one of its values is bad.</exception>"""
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: 'keys' must be a list of tables")
    keys: dict[tuple[int, int], KeyConfig] = {}
    for index, table in enumerate(raw):
        here = f"{where} keys[{index}]"
        if not isinstance(table, dict):
            raise ConfigError(f"{here}: must be a table")
        row = _int(table, "row", here, None, 0, ROWS - 1)
        column = _int(table, "column", here, None, 0, KEY_COLUMNS - 1)
        if (row, column) in keys:
            raise ConfigError(f"{here}: row {row} column {column} is assigned twice")
        image = _str(table, "image", here)
        animation = table.get("animation")

        def action_for(field: str, label: str):
            """
            <summary>
            Parse the action stored under a field of the current table, or None when it is absent.
            </summary>
            <param name="field">Key in the table.</param>
            <param name="label">Name for the error message.</param>
            <returns>A parsed action or None.</returns>
            """
            raw = table.get(field)
            return _parse_action(raw, f"{here} {label}") if raw is not None else None

        keys[(row, column)] = KeyConfig(
            row=row, column=column,
            image=_resolve(image, base_dir) if image else None,
            label=_str(table, "label", here),
            action=action_for("action", "action"),
            animation=_parse_animation(animation, here) if animation is not None else None,
            background=_colour(table, "background", here),
            mark=_parse_mark(table.get("mark"), here),
            action_long=action_for("action_long", "long press action"),
            action_double=action_for("action_double", "double press action"),
        )
    return keys


def _parse_action(table: Any, where: str, nested: bool = False) -> Action:
    """<summary>
    Validate one action table and turn it into an Action with its parameters.
    </summary>
    <param name="table">The action table from the file.</param>
    <param name="where">How to name this action in an error.</param>
    <param name="nested">True when this is a step of a multi action, which
    bars the types that toggle on and off.</param>
    <returns>An Action whose params are known good for its type.</returns>
    <remarks>
    This is the single place an action's parameters are checked, and
    everything downstream reads ``params`` by name without checking again. A
    new action type that is added to ACTION_TYPES and not given a branch here
    parses into an Action with no parameters at all and then fails on the
    press, which is exactly the failure this module exists to prevent.

    ``nested`` bars hold, boost, repeat and multi as steps. The first three
    are toggles: they run until the key is pressed again, and a step that
    never finishes would strand the rest of the sequence behind it. A multi
    inside a multi is barred to keep the nesting one deep.

    An empty key list is allowed for the key sending types on purpose. The
    configuration page saves a key as soon as its type is chosen, before
    anything has been captured, and an action with nothing to send simply
    does nothing rather than refusing to load.
    </remarks>
    
    <exception cref="ConfigError">Not a table, an unknown type, a missing or
    bad parameter, or a type that cannot be nested appearing as a step.</exception>"""
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: 'action' must be a table")
    kind = _str(table, "type", where, required=True) or ""
    if kind not in ACTION_TYPES:
        raise ConfigError(f"{where}: unknown action type '{kind}', expected one of {', '.join(ACTION_TYPES)}")
    params: dict[str, Any] = {}
    # An empty key list is allowed for the key actions: the page saves a key
    # the moment its type is chosen, before anything has been captured, and
    # an action with nothing to send simply does nothing.
    if kind == "hotkey":
        params["keys"] = _combo(_optional_keys(table, where), where)
        # Zero holds nothing: the key is tapped, which is the old behaviour.
        params["hold_ms"] = _int(table, "hold_ms", where, 0, 0, HOTKEY_MAX_HOLD_MS)
    elif kind == "sequence":
        raw = table.get("keys", [])
        if isinstance(raw, str):
            raw = raw.split()
        if not isinstance(raw, list) or not all(isinstance(k, str) and k.strip() for k in raw):
            raise ConfigError(f"{where}: sequence action needs 'keys', a list of key combinations")
        params["keys"] = tuple(_combo(k, where) for k in raw)
        params["delay_ms"] = _int(table, "delay_ms", where, SEQUENCE_DEFAULT_DELAY_MS, 0, SEQUENCE_MAX_DELAY_MS)
    elif kind == "chord":
        hold = table.get("hold", "")
        if not isinstance(hold, str):
            raise ConfigError(f"{where}: 'hold' must be text")
        params["hold"] = _combo(hold, where)
        raw = table.get("keys", [])
        if isinstance(raw, str):
            raw = raw.split()
        if not isinstance(raw, list) or not all(isinstance(k, str) and k.strip() for k in raw):
            raise ConfigError(f"{where}: chord action needs 'keys', a list of key combinations")
        params["keys"] = tuple(_combo(k, where) for k in raw)
        for name, default in CHORD_DEFAULTS.items():
            params[name] = _int(table, name, where, default, 0, SEQUENCE_MAX_DELAY_MS)
        if params["delay_min_ms"] > params["delay_max_ms"]:
            raise ConfigError(f"{where}: delay_min_ms must not exceed delay_max_ms")
    elif kind == "hold":
        if nested:
            raise ConfigError(f"{where}: a hold action cannot be a step of a multi action")
        params["keys"] = _combo(_optional_keys(table, where), where)
        for name, default in HOLD_DEFAULTS.items():
            params[name] = _int(table, name, where, default, 0, HOLD_MAX_MS)
        if params["hold_min_ms"] > params["hold_max_ms"]:
            raise ConfigError(f"{where}: hold_min_ms must not exceed hold_max_ms")
        if params["release_min_ms"] > params["release_max_ms"]:
            raise ConfigError(f"{where}: release_min_ms must not exceed release_max_ms")
        if params["hold_max_ms"] == 0:
            raise ConfigError(f"{where}: hold_max_ms must be above zero")
    elif kind == "boost":
        # One key held down the whole time (forward), while a second is pressed
        # for on_ms and let go for off_ms, over and over, until it is switched
        # off by pressing the deck key again.
        if nested:
            raise ConfigError(f"{where}: a boost action cannot be a step of a multi action")
        hold = table.get("hold", "")
        if not isinstance(hold, str):
            raise ConfigError(f"{where}: 'hold' must be text")
        params["hold"] = _combo(hold, where)
        params["keys"] = _combo(_optional_keys(table, where), where)
        for name, default in BOOST_DEFAULTS.items():
            params[name] = _int(table, name, where, default, 0, HOLD_MAX_MS)
        if params["on_ms"] == 0:
            raise ConfigError(f"{where}: on_ms must be above zero")
    elif kind == "repeat":
        # Tap a key, wait every_ms, tap it again, until it is switched off.
        if nested:
            raise ConfigError(f"{where}: a repeat action cannot be a step of a multi action")
        params["keys"] = _combo(_optional_keys(table, where), where)
        params["every_ms"] = _int(table, "every_ms", where, REPEAT_DEFAULT_EVERY_MS, 0, HOLD_MAX_MS)
        if params["every_ms"] == 0:
            raise ConfigError(f"{where}: every_ms must be above zero")
    elif kind == "launch":
        params["command"] = _str(table, "command", where, required=True)
        for override in ("command_windows", "command_linux"):
            value = _str(table, override, where)
            if value:
                params[override] = value
    elif kind == "url":
        params["url"] = _str(table, "url", where, required=True)
    elif kind == "page":
        params["page"] = _str(table, "page", where, required=True)
    elif kind == "brightness":
        if "value" in table:
            params["value"] = _int(table, "value", where, None, 0, 100)
        elif "delta" in table:
            params["delta"] = _int(table, "delta", where, None, -100, 100)
        else:
            raise ConfigError(f"{where}: brightness action needs 'value' or 'delta'")
    elif kind == "multi":
        if nested:
            raise ConfigError(f"{where}: a multi action cannot contain another multi action")
        steps = table.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ConfigError(f"{where}: multi action needs a non empty 'steps' list")
        params["steps"] = tuple(_parse_action(step, f"{where} steps[{i}]", nested=True)
                                for i, step in enumerate(steps))
        params["delay_ms"] = _int(table, "delay_ms", where, 0, 0, 60_000)
    return Action(type=kind, params=params)


def _optional_keys(table: dict, where: str) -> str:
    """
    <summary>
    The optional keys text of a table, stripped; empty when absent.
    </summary>
    <param name="table">The table.</param>
    <param name="where">Location for the error message.</param>
    <returns>A string.</returns>
    <exception cref="ConfigError">When keys is present but not text.</exception>
    """
    value = table.get("keys", "")
    if not isinstance(value, str):
        raise ConfigError(f"{where}: 'keys' must be text")
    return value.strip()


def _combo(text: str, where: str) -> str:
    """<summary>
    Check a key combination now, so a typo is reported at load rather than on press.
    </summary>
    <param name="text">A combination such as "ctrl+shift+f5", possibly empty.</param>
    <param name="where">How to name this place in an error.</param>
    <returns>The combination trimmed, or "" when there was nothing to check.</returns>
    <remarks>
    The parsed form is thrown away and the text is kept, because the hotkeys
    module parses it again when the key is pressed. This is the check, not
    the conversion.

    Empty is allowed and returns empty, which is how a half configured key
    from the configuration page gets through. The hotkeys import is inside
    the function to keep the platform specific input code out of anything
    that only wants to read a config.
    </remarks>
    
    <exception cref="ConfigError">The combination names a key or a modifier
    the hotkeys module does not know.</exception>"""
    from . import hotkeys

    text = text.strip()
    if not text:
        return ""
    try:
        hotkeys.parse_combo(text)
    except hotkeys.HotkeyError as err:
        raise ConfigError(f"{where}: {err}") from err
    return text


def _check_page_targets(action: Action | None, names: set[str], where: str) -> None:
    """<summary>
    Refuse a page action that jumps to a page which does not exist.
    </summary>
    <param name="action">Any action, or None, which is ignored.</param>
    <param name="names">Every page name in this config.</param>
    <param name="where">How to name the key this action belongs to.</param>
    <remarks>
    Runs only after every page has been built, since a page may jump forwards
    to one defined later in the file. Recurses into a multi action's steps, so
    a jump buried in a step is caught as firmly as a plain one; the nesting is
    only ever one deep, so the recursion cannot run away.

    Without this, a renamed or deleted page turns into a key that does
    nothing at all when pressed, with nothing in the log to say why.
    </remarks>
    
    <exception cref="ConfigError">A page action names something that is
    neither a page nor one of PAGE_TARGETS.</exception>"""
    if action is None:
        return
    if action.type == "page":
        target = action.params["page"]
        if target not in names and target not in PAGE_TARGETS:
            raise ConfigError(f"{where}: page '{target}' does not exist")
    elif action.type == "multi":
        for step in action.params["steps"]:
            _check_page_targets(step, names, where)


def _resolve(value: str, base_dir: Path) -> Path:
    """<summary>
    Turn an image value from the file into a path on this machine.
    </summary>
    <param name="value">A ``theme:`` reference, or an absolute or relative path.</param>
    <param name="base_dir">The folder a relative path is resolved against.</param>
    <returns>A path, which may well not exist.</returns>
    <remarks>
    Never raises and never checks that the file is there. A missing picture
    draws the missing marker on the key, and refusing to load a whole config
    because one image has been moved would be far worse than one key showing
    a marker.

    Relative paths resolve against the config file's folder and not the
    working directory, so a config folder with its images beside it can be
    copied to another machine and still work. ``~`` is expanded, so a path
    written with one follows whoever is running the daemon.
    </remarks>
    """
    parsed = themes.parse_ref(value)
    if parsed is not None:
        # A shipped theme icon. Keep the intended path even when the file is
        # absent so a stale reference draws the missing marker, not a guess.
        return themes.icon_path(*parsed, must_exist=False) or themes.THEMES_DIR / "unknown.png"
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base_dir / path)


# The document form: the same config as plain dicts and lists, which is what
# the configuration page edits as JSON. Going back to TOML uses a small
# emitter below, because the standard library can only read TOML.


def _relative(path: Path | None, base_dir: Path) -> str | None:
    """<summary>
    The shortest honest way to write a path back into the config file.
    </summary>
    <param name="path">A resolved path, or None.</param>
    <param name="base_dir">The config file's folder.</param>
    <returns>A theme reference, a relative path, or an absolute one.</returns>
    <remarks>
    The three answers are tried in that order and the first that fits wins.
    A packaged icon becomes a ``theme:`` reference so the saved config works
    on a machine where the install lives somewhere else; anything inside the
    config folder becomes relative so the folder can be moved as a unit;
    anything else stays absolute because there is no honest shorter form.

    Relative paths are written with forward slashes whatever the platform,
    which TOML and both operating systems accept, so a config saved on
    Windows still reads on Linux.
    </remarks>
    """
    if path is None:
        return None
    ref = themes.ref_for(path)
    if ref is not None:
        return ref
    try:
        return path.relative_to(base_dir).as_posix()
    except ValueError:
        return path.as_posix()


def _action_document(action: Action | None) -> dict | None:
    """<summary>
    An action as plain dicts and lists, for the configuration page.
    </summary>
    <param name="action">The action, or None.</param>
    <returns>A dict of the type and its parameters, or None.</returns>
    <remarks>
    Tuples become lists because the document form is edited as JSON, which
    has no tuple, and a tuple would not survive the round trip anyway. Steps
    are recursed into by name rather than by type, since a multi action's
    steps are the only place another action is nested.

    Whatever is in ``params`` is written out as it stands. That is only safe
    because <see cref="_parse_action"/> put it there, so nothing that cannot
    be written as TOML can have got in.
    </remarks>
    """
    if action is None:
        return None
    doc: dict[str, Any] = {"type": action.type}
    for key, value in action.params.items():
        if key == "steps":
            doc["steps"] = [_action_document(step) for step in value]
        elif isinstance(value, tuple):
            doc[key] = list(value)
        else:
            doc[key] = value
    return doc


def _animation_document(animation: AnimationConfig | None) -> dict | None:
    """
    <summary>
    An animation config as the plain dict the web document carries, or None.
    </summary>
    <param name="animation">The config, or None.</param>
    <returns>A dict or None.</returns>
    """
    if animation is None:
        return None
    return {"kind": animation.kind, "colour": animation.colour, "speed": animation.speed, "fps": animation.fps}


def document_from_config(config: Config) -> dict:
    """<summary>
    A whole Config as plain dicts and lists, which is what the configuration
    page edits.
    </summary>
    <param name="config">A validated config.</param>
    <returns>A structure of nothing but dicts, lists, strings, numbers and
    booleans, ready to be serialised as JSON.</returns>
    <remarks>
    The other half of the round trip: this out, <see cref="to_toml"/> to
    write, then <see cref="parse"/> to read back, and a config that goes all
    the way round must come back the same. The tests hold that, and it is
    what stops the configuration page quietly losing a setting it does not
    know about.

    Keys are sorted by position rather than kept in file order, so a saved
    config has a predictable shape and two saves of the same config produce
    the same text. A key that is absent is written as None here and dropped
    at the TOML stage, rather than being left out now, which keeps the shape
    the page receives the same for every key.

    Paths come back as they should be written, not as they were resolved, so
    the base_dir on the config has to be the one the paths were resolved
    against or the saved file will point at the wrong folder.
    </remarks>
    """
    pages = []
    for page in config.pages:
        keys = []
        for (row, column), key in sorted(page.keys.items()):
            keys.append({
                "row": row, "column": column,
                "image": _relative(key.image, config.base_dir),
                "label": key.label,
                "action": _action_document(key.action),
                "action_long": _action_document(key.action_long),
                "action_double": _action_document(key.action_double),
                "animation": _animation_document(key.animation),
                "background": key.background,
                "mark": _mark_document(key.mark),
            })
        pages.append({"name": page.name, "keys": keys, "match_window": page.match_window,
                      "wallpaper": _relative(page.wallpaper, config.base_dir)})
    strip: list[Any] = []
    for tile in config.strip:
        if tile.kind == "image":
            entry: Any = {"image": _relative(tile.image, config.base_dir), "fit": tile.fit}
        elif tile.kind == "animation":
            entry = {"animation": _animation_document(tile.animation)}
        elif tile.background:
            entry = {"kind": tile.kind}
        else:
            entry = tile.kind
        if tile.background and isinstance(entry, dict):
            entry["background"] = tile.background
        strip.append(entry)
    return {
        "strip_visible": {"width": config.strip_visible[0], "height": config.strip_visible[1]},
        "deck": {
            "brightness": config.deck.brightness,
            "sleep_after_minutes": config.deck.sleep_after_minutes,
            "official_software": config.deck.official_software,
            "press_flash": config.deck.press_flash,
            "background": config.deck.background,
            "long_press_ms": config.deck.long_press_ms,
            "double_press_ms": config.deck.double_press_ms,
            "follow_focus": config.deck.follow_focus,
            "focus_poll_ms": config.deck.focus_poll_ms,
            "key_pitch_x": config.deck.key_pitch_x,
            "key_pitch_y": config.deck.key_pitch_y,
        },
        "server": {
            "enabled": config.server.enabled,
            "bind": config.server.bind,
            "port": config.server.port,
            "token": config.server.token,
        },
        "weather": {
            "latitude": config.weather.latitude,
            "longitude": config.weather.longitude,
            "location_name": config.weather.location_name,
            "units": config.weather.units,
            "refresh_minutes": config.weather.refresh_minutes,
        },
        "strip": strip,
        "pages": pages,
    }


def _toml_string(value: str) -> str:
    """<summary>
    One string as a quoted TOML basic string, with everything escaped.
    </summary>
    <param name="value">Any text, including a Windows path or a label the
    user typed.</param>
    <returns>The value in double quotes, safe to paste into a TOML file.</returns>
    <remarks>
    Backslash before quote in the ordering, because escaping the quotes first
    and then the backslashes would escape the backslashes this function had
    just added and double them.

    Control characters are written as the six character escape TOML defines
    rather than being passed through, since a raw one in a basic string is
    not valid TOML and would make the file unreadable. Delete is escaped with
    them: it is not a control character by the usual test but TOML bars it
    all the same.
    </remarks>
    """
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_value(value: Any) -> str:
    """<summary>
    Any document value as the TOML text for it.
    </summary>
    <param name="value">A bool, number, string, list, tuple or dict.</param>
    <returns>The value as TOML, inline for a list or a dict.</returns>
    <remarks>
    The bool test comes before the int test and must stay there, because
    Python counts a bool as an int and ``True`` would otherwise be written as
    ``1`` and read back as a number.

    Infinity and not a number are refused rather than written. TOML can spell
    them, but nothing in a config has any business being either, so reaching
    one means something upstream went wrong and saying so beats writing a
    file that loads and then behaves strangely.

    None is never handled here: the callers drop a None value before it gets
    this far, because TOML has no way to write one.
    </remarks>
    
    <exception cref="ConfigError">The value is of a type that cannot be
    written, or is a float that is not finite.</exception>"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ConfigError("numbers must be finite")
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        items = [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items() if v is not None]
        return "{ " + ", ".join(items) + " }"
    raise ConfigError(f"cannot write a value of type {type(value).__name__}")


def _toml_key(key: str) -> str:
    """
    <summary>
    A key as TOML writes it: bare when every character is allowed bare, quoted otherwise.
    </summary>
    <param name="key">The key.</param>
    <returns>A string.</returns>
    """
    if key and all(ch.isalnum() or ch in "_-" for ch in key):
        return key
    return _toml_string(key)


def _toml_lines(table: dict) -> list[str]:
    """<summary>
    A flat table as one ``key = value`` line each, dropping anything None.
    </summary>
    <param name="table">A dict of plain values, no nesting.</param>
    <returns>One line per key that has a value.</returns>
    <remarks>
    Dropping None is what turns an unset value into an absent key rather than
    into something TOML cannot write, and it is why a config saved twice does
    not grow. It also means this cannot be used to clear a setting by writing
    None over it: the line simply disappears and the default comes back.
    </remarks>
    """
    return [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in table.items() if v is not None]


def to_toml(document: dict) -> str:
    """<summary>
    Write the document form back out as TOML. Comments are not kept.
    </summary>
    <param name="document">A document as built by
    <see cref="document_from_config"/>, or the same shape from the
    configuration page.</param>
    <returns>The whole file as text, ending in a single newline.</returns>
    <remarks>
    Comments and the ordering the user chose are both lost, because this
    writes the document out afresh rather than editing the file in place.
    Anyone who hand edits their config and then saves from the configuration
    page gets their comments removed, which is worth saying out loud in the
    interface and not only here.

    The output is not validated. Parse it through <see cref="parse"/> before
    it replaces a working file, so a document that would not load is caught
    while the good file is still there.

    Sections are written in a fixed order with a blank line between them, and
    the writer is small on purpose: the standard library can only read TOML,
    and nothing here needs the parts of the format a full emitter would have
    to cover.
    </remarks>
    
    <exception cref="ConfigError">The document is not an object, its pages
    are not a list, or a value cannot be written as TOML.</exception>"""
    if not isinstance(document, dict):
        raise ConfigError("document must be an object")
    lines = ["# Deckplate configuration, written by the configuration page.",
             "# Positions are row and column from the top left. See README.md for the fields.",
             ""]
    for section in ("deck", "server", "weather"):
        table = document.get(section)
        if isinstance(table, dict) and table:
            lines.append(f"[{section}]")
            lines.extend(_toml_lines(table))
            lines.append("")
    strip = document.get("strip")
    visible = document.get("strip_visible")
    if strip is not None or visible is not None:
        lines.append("[strip]")
        if isinstance(visible, dict):
            if visible.get("width") is not None:
                lines.append(f"visible_width = {_toml_value(visible['width'])}")
            if visible.get("height") is not None:
                lines.append(f"visible_height = {_toml_value(visible['height'])}")
        if strip is not None:
            lines.append(f"tiles = {_toml_value(strip)}")
        lines.append("")
    pages = document.get("pages")
    if not isinstance(pages, list):
        raise ConfigError("'pages' must be a list")
    for page in pages:
        if not isinstance(page, dict):
            raise ConfigError("each page must be an object")
        lines.append("[[pages]]")
        lines.append(f"name = {_toml_value(page.get('name', ''))}")
        if page.get("match_window"):
            lines.append(f"match_window = {_toml_value(page['match_window'])}")
        if page.get("wallpaper"):
            lines.append(f"wallpaper = {_toml_value(page['wallpaper'])}")
        lines.append("")
        keys = page.get("keys") or []
        if not isinstance(keys, list):
            raise ConfigError("'keys' must be a list")
        for key in keys:
            if not isinstance(key, dict):
                raise ConfigError("each key must be an object")
            lines.append("[[pages.keys]]")
            for field in ("row", "column", "image", "label", "background", "mark",
                          "action", "action_long", "action_double", "animation"):
                if key.get(field) is not None:
                    lines.append(f"{field} = {_toml_value(key[field])}")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
