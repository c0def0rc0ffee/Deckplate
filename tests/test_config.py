"""<summary>
Reading a configuration file: defaults, validation, and the messages a user
sees when they have got something wrong.
</summary>
<remarks>
The configuration is hand edited TOML as well as something the configuration
page writes, so almost everything here is about being wrong helpfully. Every
fault must be caught at load, with a message naming the field, because the
alternative is a daemon that starts and then misbehaves on one key with
nothing in the log to say why.

The base directory is passed as the string "/base" in most tests and as a
Path in a few. That mixture is deliberate: both are real callers, and the
parser must accept either. Nothing under it is ever touched on disk.

Where a test lists bad inputs, it matches on the message text and not just
on the exception type. The message is the entire value of the check, so a
test that accepted any error would pass against a parser that reported
every fault as a generic failure.
</remarks>
"""

from pathlib import Path

import pytest

from deckplate import config as cfg

# The smallest file the parser will accept: one page and nothing else.
# Used wherever a test needs valid surroundings for the fragment it cares about.
MINIMAL = """
[[pages]]
name = "Main"
"""

# One file exercising every section at once, so the shape tests can assert
# against a single realistic configuration rather than a dozen fragments.
FULL = """
[deck]
brightness = 55
sleep_after_minutes = 0

[weather]
latitude = 49.45
longitude = -2.54
units = "imperial"
refresh_minutes = 15
location_name = "Guernsey"

[strip]
tiles = ["clock", "images/logo.png", { image = "/abs/pic.png" }]

[[pages]]
name = "Main"

[[pages.keys]]
row = 0
column = 4
image = "images/web.png"
label = "Web"
action = { type = "url", url = "https://example.org" }

[[pages.keys]]
row = 2
column = 4
action = { type = "page", page = "Second" }

[[pages.keys]]
row = 1
column = 1
action = { type = "multi", delay_ms = 50, steps = [
    { type = "hotkey", keys = "ctrl+l" },
    { type = "launch", command = "gedit", command_windows = "notepad.exe" },
    { type = "brightness", delta = -10 },
    { type = "sleep" },
] }

[[pages]]
name = "Second"

[[pages.keys]]
row = 0
column = 0
action = { type = "page", page = "previous" }
"""


def test_minimal_config_gets_defaults():
    """<summary>
    Pins what a user gets when they write almost nothing: a working deck,
    with every default filled in.
    </summary>
    <remarks>
    Two of these defaults are policy rather than convenience. Sleeping after
    zero minutes means never sleep automatically, because a deck that blanks
    itself unasked looks broken. Asking about rival software is the safe
    middle: neither killing another program without permission nor silently
    fighting it for the device.

    The three default strip panels are pinned because the strip must always
    hold exactly three, so an absent strip section cannot mean an empty one.
    </remarks>
    """
    c = cfg.parse(MINIMAL, "/base")
    assert c.deck.brightness == 80
    assert c.deck.sleep_after_minutes == 0  # default is never auto sleep
    assert c.deck.official_software == "ask"
    assert c.weather.units == "metric"
    assert [t.kind for t in c.strip] == ["clock", "date", "weather"]
    assert len(c.pages) == 1 and c.pages[0].keys == {}


def test_full_config_round_trip():
    """<summary>
    Pins a realistic file parsing into the structure the rest of the daemon
    expects, down to nested action steps.
    </summary>
    <remarks>
    The image paths carry the real content here. A relative path is resolved
    against the base directory and an absolute one is left exactly as
    written, because a user who typed an absolute path meant somewhere
    outside the configuration folder. Getting that wrong gives keys with no
    picture and no error.

    Keys are looked up by a row and column pair rather than by position in
    the file, which is what lets the page draw the deck. The multi step
    covers two things at once: the steps keeping their order, and a per
    platform override surviving inside a nested step, where it is furthest
    from the code that normally handles it.
    </remarks>
    """
    c = cfg.parse(FULL, Path("/base"))
    assert c.deck.brightness == 55
    assert c.weather.location_name == "Guernsey" and c.weather.units == "imperial"
    assert c.strip[1].kind == "image" and c.strip[1].image == Path("/base/images/logo.png")
    assert c.strip[2].image == Path("/abs/pic.png")

    main = c.page_named("Main")
    web = main.keys[(0, 4)]
    assert web.image == Path("/base/images/web.png")
    assert web.label == "Web"
    assert web.action.type == "url" and web.action.params["url"] == "https://example.org"

    multi = main.keys[(1, 1)].action
    assert multi.type == "multi" and multi.params["delay_ms"] == 50
    assert [s.type for s in multi.params["steps"]] == ["hotkey", "launch", "brightness", "sleep"]
    assert multi.params["steps"][1].params["command_windows"] == "notepad.exe"
    assert c.page_named("Second").keys[(0, 0)].action.params["page"] == "previous"


@pytest.mark.parametrize("text, message", [
    ("", "at least one"),
    ("[[pages]]\nname = 'A'\n[[pages]]\nname = 'A'", "used twice"),
    ("[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 3\ncolumn = 0", "between 0 and 2"),
    ("[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 0\ncolumn = 5", "between 0 and 4"),
    ("[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 0\ncolumn = 0\n[[pages.keys]]\nrow = 0\ncolumn = 0", "twice"),
    ("[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 0\ncolumn = 0\naction = { type = 'dance' }", "unknown action"),
    ("[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 0\ncolumn = 0\naction = { type = 'page', page = 'Nope' }", "does not exist"),
    ("[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 0\ncolumn = 0\naction = { type = 'brightness' }", "value"),
    ("[strip]\ntiles = ['clock']\n[[pages]]\nname = 'A'", "exactly 3"),
    ("[weather]\nunits = 'kelvin'\n[[pages]]\nname = 'A'", "metric"),
    ("[deck]\nbrightness = 120\n[[pages]]\nname = 'A'", "between 0 and 100"),
    ("[deck]\nofficial_software = 'maybe'\n[[pages]]\nname = 'A'", "official_software"),
    ("this is not toml = = =", "not valid TOML"),
])
def test_errors_name_the_problem(text, message):
    """<summary>
    Pins the whole table of structural faults as refusals whose message
    names the actual problem.
    </summary>
    <remarks>
    Each row is a mistake a person makes by hand. The physical limits are
    the deck's own three rows and five columns, so those are facts and not
    preferences. A duplicate page name and a duplicate key position have to
    be refused rather than resolved, because either way of resolving them
    silently discards something the user wrote.

    A page action naming a page that does not exist is caught here, at load,
    and this is the important one: it cannot be checked until every page has
    been read, and leaving it uncaught gives a key that does nothing at all
    with no clue why. Invalid TOML is included so that a syntax error
    arrives as a configuration error like everything else, rather than as a
    parser exception from a library the user has never heard of.
    </remarks>
    """
    with pytest.raises(cfg.ConfigError, match=message):
        cfg.parse(text, "/base")


def test_sequence_action_parses_and_validates():
    """<summary>
    Pins both spellings of a key sequence, and the default delay applied
    when none is given.
    </summary>
    <remarks>
    A list is what the configuration page writes. A single space separated
    string is what a person types, and the double space in the sample is
    there on purpose: splitting on runs of whitespace rather than on a
    single space is what stops an empty key appearing in the middle of the
    sequence. Both forms must land on the same tuple, so the rest of the
    daemon never has to care which was written.
    </remarks>
    """
    text = """
[[pages]]
name = "A"
[[pages.keys]]
row = 0
column = 0
action = { type = "sequence", keys = ["ctrl+l", "h", "enter"], delay_ms = 80 }
[[pages.keys]]
row = 0
column = 1
action = { type = "sequence", keys = "a b  c" }
"""
    c = cfg.parse(text, "/base")
    first = c.pages[0].keys[(0, 0)].action
    assert first.params == {"keys": ("ctrl+l", "h", "enter"), "delay_ms": 80}
    second = c.pages[0].keys[(0, 1)].action
    assert second.params == {"keys": ("a", "b", "c"), "delay_ms": cfg.SEQUENCE_DEFAULT_DELAY_MS}


def test_chord_action_parses_with_defaults():
    """<summary>
    Pins a chord filling in its defaults, and pins that a chord nested
    inside a multi is validated the same way as one at the top.
    </summary>
    <remarks>
    The defaults are spread from the shared constant rather than written out
    again, so changing a default cannot leave this test asserting the old
    value while appearing to check it. The nested case matters because
    parsing an action inside a step is a different code path from parsing
    one on a key, and a fault there shows up only in a layout that happens
    to use a multi.
    </remarks>
    """
    text = """
[[pages]]
name = "A"
[[pages.keys]]
row = 0
column = 0
action = { type = "chord", hold = "alt", keys = ["1", "2"] }
[[pages.keys]]
row = 0
column = 1
action = { type = "multi", steps = [ { type = "chord", hold = "shift", keys = "a b", delay_min_ms = 10, delay_max_ms = 20 } ] }
"""
    keys = cfg.parse(text, "/base").pages[0].keys
    assert keys[(0, 0)].action.params == {"hold": "alt", "keys": ("1", "2"), **cfg.CHORD_DEFAULTS}
    step = keys[(0, 1)].action.params["steps"][0]
    assert step.params == {"hold": "shift", "keys": ("a", "b"), "delay_min_ms": 10, "delay_max_ms": 20}


def test_hold_action_defaults_and_limits():
    """<summary>
    Pins a bare hold picking up all four of its timing defaults.
    </summary>
    <remarks>
    A hold written by hand usually names only the key, and the four timings
    have to arrive complete or the hold manager is handed a None where it
    expects a range. Spreading the shared constant keeps this in step with
    whatever the defaults become.
    </remarks>
    """
    text = "[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 0\ncolumn = 0\naction = { type = 'hold', keys = 'w' }"
    action = cfg.parse(text, "/base").pages[0].keys[(0, 0)].action
    assert action.params == {"keys": "w", **cfg.HOLD_DEFAULTS}


def test_key_actions_may_be_empty_while_being_set_up():
    """<summary>
    Pins a half finished key as valid: an action with nothing captured yet
    must load rather than refuse the whole file.
    </summary>
    <remarks>
    This is the shape the configuration page saves while the user is still
    working. If the parser refused it, adding a key and saving before
    choosing its keystrokes would make the file unloadable, and the daemon
    would drop the whole configuration over one unfinished key.

    The empty value has to keep its type: a hotkey holds an empty string and
    a sequence an empty tuple, because the action layer tests emptiness on
    what it was given. A chord with neither side captured is the freshest
    case of all and is checked separately. A launch with no command is the
    same shape: choosing "Launch a program" on the page saved a key the
    parser then refused, and the page sat on "not saved" until a command
    was typed.
    </remarks>
    """
    text = """
[[pages]]
name = "A"
[[pages.keys]]
row = 0
column = 0
action = { type = "hotkey" }
[[pages.keys]]
row = 0
column = 1
action = { type = "sequence", keys = [] }
[[pages.keys]]
row = 0
column = 2
action = { type = "hold", keys = "" }
[[pages.keys]]
row = 0
column = 3
action = { type = "launch" }
"""
    keys = cfg.parse(text, "/base").pages[0].keys
    assert keys[(0, 0)].action.params["keys"] == ""
    assert keys[(0, 1)].action.params["keys"] == ()
    assert keys[(0, 2)].action.params["keys"] == ""
    # a launch fresh from the page: the type chosen, no command typed yet
    assert keys[(0, 3)].action.params["command"] == ""
    # a chord fresh from the page: nothing captured on either side yet
    text = '[[pages]]\nname = "A"\n[[pages.keys]]\nrow = 0\ncolumn = 0\naction = { type = "chord", hold = "", keys = [] }'
    chord = cfg.parse(text, "/base").pages[0].keys[(0, 0)].action
    assert chord.params["hold"] == "" and chord.params["keys"] == ()


@pytest.mark.parametrize("action, message", [
    ("{ type = 'sequence', keys = 5 }", "list of key combinations"),
    ("{ type = 'hotkey', keys = 5 }", "must be text"),
    ("{ type = 'hold', keys = 'w', hold_min_ms = 900, hold_max_ms = 100 }", "hold_min_ms"),
    ("{ type = 'chord', hold = 'alt', keys = ['1'], delay_min_ms = 500, delay_max_ms = 100 }", "delay_min_ms"),
    ("{ type = 'chord', hold = 'banana', keys = ['1'] }", "unknown key"),
    ("{ type = 'hold', keys = 'w', release_min_ms = 900, release_max_ms = 100 }", "release_min_ms"),
    ("{ type = 'hold', keys = 'w', hold_min_ms = 0, hold_max_ms = 0 }", "above zero"),
    ("{ type = 'multi', steps = [ { type = 'hold', keys = 'w' } ] }", "cannot be a step"),
    ("{ type = 'sequence', keys = ['ctrl+banana'] }", "unknown key"),
    ("{ type = 'sequence', keys = ['a'], delay_ms = 99999 }", "between 0 and 10000"),
    ("{ type = 'hotkey', keys = 'ctrl+' }", "empty key"),
])
def test_bad_key_combinations_are_reported_at_load(action, message):
    """<summary>
    Pins the whole table of bad keystroke definitions as refusals at load,
    each naming the field at fault.
    </summary>
    <remarks>
    An unknown key name is the reason this check exists at all. Without it a
    typed key name reaches the keyboard backend at the moment the user
    presses the deck key, where it either raises inside the event loop or
    does nothing, and the user has no way to connect either outcome to a
    typing mistake made weeks earlier. Refusing at load names the file and
    the field instead.

    An inverted range, where the minimum exceeds the maximum, is refused
    rather than quietly swapped, because it always means the user typed the
    pair the wrong way round and swapping them hides that. A hold span of
    zero is refused because a hold that holds for no time is a hold that
    spins a thread doing nothing at full speed.

    A toggling action inside a multi is refused for a different reason
    entirely: a multi runs once and moves on, while a hold stays on until
    something switches it off, and there is nothing in a multi that could.
    A trailing plus is caught because splitting it yields an empty final
    key, which the backend would reject far downstream.
    </remarks>
    """
    text = f"[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 0\ncolumn = 0\naction = {action}"
    with pytest.raises(cfg.ConfigError, match=message):
        cfg.parse(text, "/base")


def test_nested_multi_is_rejected():
    """<summary>
    Pins a multi inside a multi as refused.
    </summary>
    <remarks>
    Nesting is refused so that a multi's total duration can be reasoned
    about and so the runner has no recursion to guard against. It also
    closes the door on a configuration that refers to itself indirectly,
    which would hang the event loop on a press rather than raising.
    </remarks>
    """
    text = """
[[pages]]
name = "A"
[[pages.keys]]
row = 0
column = 0
action = { type = "multi", steps = [ { type = "multi", steps = [ { type = "sleep" } ] } ] }
"""
    with pytest.raises(cfg.ConfigError, match="another multi"):
        cfg.parse(text, "/base")


def test_load_and_ensure_config(tmp_path):
    """<summary>
    Pins first run setup: a configuration and an images folder are created
    where none exist, and an existing file is never overwritten.
    </summary>
    <remarks>
    The last three lines are the ones that matter. Running again must leave
    the user's file byte for byte as it was, because this is called on every
    start: a version that rewrote the file would destroy a hand edited
    configuration the first time the daemon was restarted, with no warning
    and no copy kept.

    Missing parent directories are created, since the path can come from a
    command line argument pointing anywhere. The images folder is created
    alongside because relative image paths are resolved against it, and an
    absent folder would make every example picture fail to load.
    </remarks>
    """
    path = cfg.ensure_config(tmp_path / "sub" / "config.toml")
    assert path.exists()
    assert (tmp_path / "sub" / "images").is_dir()
    c = cfg.load(path)
    assert c.path == path and c.base_dir == path.parent
    assert c.page_named("Main") is not None
    # calling again leaves the file alone
    path.write_text(MINIMAL, encoding="utf-8")
    cfg.ensure_config(path)
    assert path.read_text(encoding="utf-8") == MINIMAL


def test_animation_settings_parse_and_validate():
    """<summary>
    Pins the short and long forms of an animation, on both keys and strip
    panels, and the four ways of getting one wrong.
    </summary>
    <remarks>
    A bare name is the short form and a table is the long one, and both must
    end at the same structure so nothing downstream has to test which was
    written. The speed of 2 in the sample is an integer in the file and must
    come back as a float, since the renderer multiplies by it.

    The rejections are all values a person would think reasonable: a made up
    effect name, a colour by name rather than in hex, a speed beyond the
    slider's range, and a frame rate higher than the panels can show. Each
    is refused at load with the field named, rather than being clamped
    silently, because a clamped value reappears in the configuration page
    as something the user did not type.
    </remarks>
    """
    text = """
[deck]
press_flash = false
[strip]
tiles = ["clock", { animation = { kind = "wave", colour = "#fff", speed = 2 } }, { animation = "rainbow" }]
[[pages]]
name = "A"
[[pages.keys]]
row = 0
column = 0
animation = "pulse"
[[pages.keys]]
row = 0
column = 1
animation = { kind = "scroll", colour = "#4caf7d", speed = 0.5, fps = 12 }
"""
    c = cfg.parse(text, "/base")
    assert c.deck.press_flash is False
    assert c.strip[1].kind == "animation" and c.strip[1].animation == cfg.AnimationConfig("wave", "#fff", 2.0, 20)
    assert c.strip[2].animation.kind == "rainbow"
    keys = c.pages[0].keys
    assert keys[(0, 0)].animation == cfg.AnimationConfig("pulse")
    assert keys[(0, 1)].animation == cfg.AnimationConfig("scroll", "#4caf7d", 0.5, 12)
    for bad, message in [
        ("animation = 'disco'", "unknown animation"),
        ("animation = { kind = 'pulse', colour = 'blue' }", "colour"),
        ("animation = { kind = 'pulse', speed = 9 }", "speed"),
        ("animation = { kind = 'pulse', fps = 90 }", "between 1 and 30"),
    ]:
        with pytest.raises(cfg.ConfigError, match=message):
            cfg.parse(f"[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 0\ncolumn = 0\n{bad}", "/base")


def test_background_colours_parse_and_validate():
    """<summary>
    Pins background colours at all three levels, the lower cased storage,
    and an unset one staying None so it can inherit.
    </summary>
    <remarks>
    Colours are stored exactly as written except for case, which is folded
    down so that two spellings of the same colour compare equal; the sample
    uses capitals for exactly that reason. The short three digit form is
    kept as written rather than expanded, since the renderer accepts both
    and rewriting it would change the user's file on the next save.

    None on a key means inherit, and it must stay None rather than being
    filled in with the deck colour. A filled in value would freeze that key
    at today's deck colour forever, and the user would find some keys no
    longer follow the deck setting with no way to tell which.
    </remarks>
    """
    text = """
[deck]
background = "#102030"
[strip]
tiles = [{ kind = "clock", background = "#000" }, { image = "images/a.png", background = "#abcdef" }, "weather"]
[[pages]]
name = "A"
[[pages.keys]]
row = 0
column = 0
background = "#FF8800"
[[pages.keys]]
row = 0
column = 1
label = "plain"
"""
    c = cfg.parse(text, "/base")
    assert c.deck.background == "#102030"
    assert c.strip[0].kind == "clock" and c.strip[0].background == "#000"
    assert c.strip[1].background == "#abcdef" and c.strip[2].background is None
    assert c.pages[0].keys[(0, 0)].background == "#ff8800"
    assert c.pages[0].keys[(0, 1)].background is None
    assert cfg.parse("[[pages]]\nname = 'A'", "/base").deck.background == cfg.DEFAULT_BACKGROUND
    with pytest.raises(cfg.ConfigError, match="background"):
        cfg.parse("[deck]\nbackground = 'navy'\n[[pages]]\nname = 'A'", "/base")
    with pytest.raises(cfg.ConfigError, match="background"):
        cfg.parse("[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 0\ncolumn = 0\nbackground = '#12'", "/base")


def test_strip_visible_area_and_fit():
    """<summary>
    Pins the measured strip window, its sanity range, and the per panel fit
    flag defaulting to off.
    </summary>
    <remarks>
    The visible width and height describe how much of each strip tile the
    bezel actually lets through, which is measured on the real deck rather
    than read from a specification. The range exists to catch a slip of a
    digit: a number in the hundreds means the whole picture is drawn outside
    what can be seen, and the panel looks blank.

    The fit flag defaulting to off matters because the two behaviours differ
    visibly: off crops a picture to the window and on scales it down inside
    it. A string where a boolean belongs is refused rather than being read
    as truthy, since "no" would otherwise mean yes.
    </remarks>
    """
    text = """
[strip]
visible_width = 82
visible_height = 76
tiles = ["clock", { image = "images/a.png", fit = true }, { image = "images/b.png" }]
[[pages]]
name = "A"
"""
    c = cfg.parse(text, "/base")
    assert c.strip_visible == (82, 76)
    assert c.strip[1].fit is True and c.strip[2].fit is False
    with pytest.raises(cfg.ConfigError, match="between 8 and 200"):
        cfg.parse("[strip]\nvisible_width = 500\n[[pages]]\nname = 'A'", "/base")
    with pytest.raises(cfg.ConfigError, match="'fit'"):
        cfg.parse("[strip]\ntiles = ['clock', { image = 'x.png', fit = 'yes' }, 'blank']\n[[pages]]\nname = 'A'", "/base")


def test_load_accepts_a_byte_order_mark(tmp_path):
    """<summary>
    Pins a file saved with a byte order mark as loading normally.
    </summary>
    <remarks>
    Several Windows editors write one without asking, and it is invisible in
    every editor afterwards. Left in place it becomes part of the first
    table name, so the file fails to parse with an error pointing at a line
    that looks perfectly correct, which is close to impossible to diagnose
    by reading.
    </remarks>
    """
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xef\xbb\xbf" + MINIMAL.encode("utf-8"))
    assert cfg.load(path).pages[0].name == "Main"


def test_default_config_resource_is_valid():
    """<summary>
    Pins the packaged starter configuration as something this very parser
    accepts.
    </summary>
    <remarks>
    That file is copied out on first run, so if it ever failed validation
    every new install would break at the first start, and the packaged file
    is the one place a fault cannot be blamed on the user. It is easy to
    edit the starter for a new feature and forget that the parser has to
    agree, which is exactly what this catches.
    </remarks>
    """
    text = cfg.DEFAULT_CONFIG_RESOURCE.read_text(encoding="utf-8")
    c = cfg.parse(text, "/base")
    assert [p.name for p in c.pages] == ["Main", "Second"]


# Second actions and per application pages


# A file carrying the extras the tests below need: both press thresholds, a
# window match on a page, and a key with a long press and a double press.
SECOND = """
[deck]
long_press_ms = 500
double_press_ms = 250

[[pages]]
name = "Main"
match_window = "firefox|chromium"

[[pages.keys]]
row = 0
column = 0
action = { type = "hotkey", keys = "ctrl+c" }
action_long = { type = "hotkey", keys = "ctrl+shift+c" }
action_double = { type = "page", page = "Second" }

[[pages]]
name = "Second"
"""


def test_second_actions_are_parsed():
    """<summary>
    Pins a key carrying three separate actions, and pins the order they come
    back in.
    </summary>
    <remarks>
    One physical key can do a different thing on a tap, on a long press and
    on a double press. The ordering from the accessor is what the controller
    and the configuration page both rely on, so it is pinned rather than
    left to whatever the structure happens to yield. The three must stay
    distinct objects: collapsing them would make a long press fire the tap
    action, which is the sort of fault that only shows up under the user's
    fingers.
    </remarks>
    """
    c = cfg.parse(SECOND, "/base")
    key = c.pages[0].keys[(0, 0)]
    assert key.action.params["keys"] == "ctrl+c"
    assert key.action_long.params["keys"] == "ctrl+shift+c"
    assert key.action_double.type == "page"
    assert key.actions() == (key.action, key.action_long, key.action_double)


def test_a_key_without_second_actions_has_none():
    """<summary>
    Pins an ordinary key leaving its long press and double press as None.
    </summary>
    <remarks>
    None is what tells the controller not to wait: with no long press bound,
    a press can act immediately instead of holding back to see whether the
    user keeps holding. If these were filled in with an empty action
    instead, every key on the deck would feel laggy by the length of the
    long press threshold.
    </remarks>
    """
    key = cfg.parse(MINIMAL + "\n[[pages.keys]]\nrow = 0\ncolumn = 0\nlabel = 'A'\n", "/base").pages[0].keys[(0, 0)]
    assert key.action_long is None and key.action_double is None


def test_the_thresholds_are_read_and_checked():
    """<summary>
    Pins the two press timings, their defaults, and the range that keeps
    them usable.
    </summary>
    <remarks>
    Both bounds are about the feel of the deck rather than about
    correctness. A threshold of a few milliseconds would make every ordinary
    tap register as a long press, and one of several seconds would make the
    user hold a key until they assumed it was broken. Neither raises
    anywhere, so the only place to catch them is here.
    </remarks>
    """
    c = cfg.parse(SECOND, "/base")
    assert c.deck.long_press_ms == 500 and c.deck.double_press_ms == 250
    assert cfg.parse(MINIMAL, "/base").deck.long_press_ms == cfg.LONG_PRESS_DEFAULT_MS
    with pytest.raises(cfg.ConfigError, match="long_press_ms"):
        cfg.parse("[deck]\nlong_press_ms = 10\n" + MINIMAL, "/base")
    with pytest.raises(cfg.ConfigError, match="double_press_ms"):
        cfg.parse("[deck]\ndouble_press_ms = 9000\n" + MINIMAL, "/base")


def test_a_bad_second_action_names_which_one_it_was():
    """<summary>
    Pins the error from a faulty long press action as saying it was the long
    press.
    </summary>
    <remarks>
    A key can hold three actions of the same shape, so a message naming only
    the row and column leaves the user checking all three. This is purely
    about the wording of the message, which is the whole point: the parser
    already refuses the file either way.
    </remarks>
    """
    with pytest.raises(cfg.ConfigError, match="long press action"):
        cfg.parse(MINIMAL + "\n[[pages.keys]]\nrow = 0\ncolumn = 0\n"
                  "action_long = { type = 'nonsense' }\n", "/base")


def test_a_page_target_in_a_second_action_is_checked():
    """<summary>
    Pins the page existence check as applying to a double press action too,
    not just to the main one.
    </summary>
    <remarks>
    The cross reference check has to run over every action slot after all
    pages are known, and the easy mistake is to run it over the main action
    only. A long or double press pointing at a deleted page would then be a
    key that silently does nothing on one of its three gestures, which is
    about as hard to notice as a fault can be.
    </remarks>
    """
    with pytest.raises(cfg.ConfigError, match="page 'Missing' does not exist"):
        cfg.parse(MINIMAL + "\n[[pages.keys]]\nrow = 0\ncolumn = 0\n"
                  "action_double = { type = 'page', page = 'Missing' }\n", "/base")


def test_match_window_is_read_and_checked():
    """<summary>
    Pins a page's window match being kept verbatim, absent by default, and
    checked as a regular expression at load.
    </summary>
    <remarks>
    The pattern is compiled here purely so a mistake is caught while the
    user is looking at the file. Left until use it would fail inside the
    focus watcher, several times a second, over a fault the user cannot see
    from the symptom: pages simply stop following the focused window. None
    rather than an empty string is what marks a page as not tied to any
    application.
    </remarks>
    """
    c = cfg.parse(SECOND, "/base")
    assert c.pages[0].match_window == "firefox|chromium"
    assert c.pages[1].match_window is None
    with pytest.raises(cfg.ConfigError, match="not a valid regular expression"):
        cfg.parse("[[pages]]\nname = 'A'\nmatch_window = '(unclosed'\n", "/base")


def test_following_the_focus_is_off_unless_asked_for():
    """<summary>
    Pins focus following as opt in, and pins a string where a boolean
    belongs as an error.
    </summary>
    <remarks>
    Switched on, the deck changes page by itself as the user moves between
    windows, which is startling if it was not asked for and needs xprop and
    an X11 session to work at all. Off by default is the right surprise free
    choice. The string case is refused rather than read as truthy, so that
    "no" cannot switch it on.
    </remarks>
    """
    assert cfg.parse(MINIMAL, "/base").deck.follow_focus is False
    assert cfg.parse("[deck]\nfollow_focus = true\n" + MINIMAL, "/base").deck.follow_focus is True
    with pytest.raises(cfg.ConfigError, match="follow_focus"):
        cfg.parse("[deck]\nfollow_focus = 'yes'\n" + MINIMAL, "/base")


def test_second_actions_and_match_window_survive_a_round_trip():
    """<summary>
    Pins the newer per key and per page settings surviving the write and
    read the configuration page performs on every save.
    </summary>
    <remarks>
    Anything the document form does not know about is dropped silently the
    first time the user presses save, so each setting added to the parser
    has to be proved across the loop as well as into it. These are
    particularly exposed: a user who set up long presses and window matching
    by hand would lose all of it by opening the page and changing a label.
    </remarks>
    """
    document = cfg.document_from_config(cfg.parse(SECOND, "/base"))
    again = cfg.parse(cfg.to_toml(document), "/base")
    assert again.pages[0].match_window == "firefox|chromium"
    key = again.pages[0].keys[(0, 0)]
    assert key.action_long.params["keys"] == "ctrl+shift+c"
    assert key.action_double.params["page"] == "Second"
    assert again.deck.long_press_ms == 500 and again.deck.double_press_ms == 250


def test_key_pitch_is_optional_and_ranged():
    """<summary>
    The pitch is unknown until measured; both numbers are needed for a
    value.
    </summary>
    <remarks>
    The pitch says how far apart the key centres sit on the one physical
    screen, and it is found by running the calibration grid rather than
    known in advance. So the pair has to be genuinely optional, and half a
    pair must read as no pitch at all: cutting tiles with one measured
    number and one guess would misplace every key on the deck.

    The last two lines are the quiet ones. A configuration with no pitch
    must emit no pitch fields whatever, not a pair of nulls, because the
    file is meant to be readable and a null invites someone to fill in a
    number by eye.
    </remarks>
    """
    assert cfg.parse(MINIMAL, "/base").deck.key_pitch is None
    c = cfg.parse("[deck]\nkey_pitch_x = 142\nkey_pitch_y = 160\n" + MINIMAL, "/base")
    assert c.deck.key_pitch == (142, 160)
    half = cfg.parse("[deck]\nkey_pitch_x = 142\n" + MINIMAL, "/base")
    assert half.deck.key_pitch_x == 142 and half.deck.key_pitch is None
    with pytest.raises(cfg.ConfigError, match="key_pitch_y"):
        cfg.parse("[deck]\nkey_pitch_y = 10\n" + MINIMAL, "/base")
    doc = cfg.document_from_config(c)
    assert doc["deck"]["key_pitch_x"] == 142 and doc["deck"]["key_pitch_y"] == 160
    again = cfg.parse(cfg.to_toml(doc), "/base")
    assert again.deck.key_pitch == (142, 160)
    unset = cfg.to_toml(cfg.document_from_config(cfg.parse(MINIMAL, "/base")))
    assert "key_pitch" not in unset


def test_page_wallpaper_is_resolved_and_round_trips():
    """<summary>
    A page may carry one picture to cut across its keys; it survives the
    document.
    </summary>
    <remarks>
    Like a key image, it is resolved against the base directory on the way
    in and written back relative, so the configuration stays portable
    between machines. The count of one occurrence in the emitted text is the
    assertion worth keeping: a page with no wallpaper must contribute no
    line at all, rather than an empty one that would parse as a picture with
    a blank path.
    </remarks>
    """
    text = MINIMAL + '\n[[pages]]\nname = "Wall"\nwallpaper = "images/bg.png"\n'
    c = cfg.parse(text, "/base")
    plain, wall = c.pages[0], c.pages[-1]
    assert plain.wallpaper is None
    assert wall.wallpaper == Path("/base/images/bg.png")
    doc = cfg.document_from_config(c)
    assert doc["pages"][0]["wallpaper"] is None and doc["pages"][-1]["wallpaper"] == "images/bg.png"
    toml = cfg.to_toml(doc)
    assert toml.count("wallpaper = ") == 1
    assert cfg.parse(toml, "/base").pages[-1].wallpaper == Path("/base/images/bg.png")


def test_theme_icon_reference_resolves_and_round_trips():
    """<summary>
    Pins the theme reference form of an image path: resolved to a packaged
    icon on the way in, and given back as the reference on the way out.
    </summary>
    <remarks>
    A packaged icon lives inside the installed application, whose location
    differs on every machine and changes with every upgrade. Writing the
    resolved absolute path into the user's file would tie their
    configuration to one install, so the reference form has to survive the
    round trip untouched.

    The file existence check is the useful extra: it proves the named icon
    is really in the package, so a theme or icon renamed in the source is
    caught here rather than as a missing picture on the deck.
    </remarks>
    """
    from pathlib import Path
    from deckplate import themes
    base = Path("/some/config/dir")
    resolved = cfg._resolve("theme:space-game/power", base)
    assert resolved == themes.icon_path("space-game", "power", must_exist=False)
    assert resolved.is_file()  # the packaged icon really exists
    # the document form gives the reference back, not an absolute path
    assert cfg._relative(resolved, base) == "theme:space-game/power"


def test_hotkey_can_be_held_for_a_time():
    """<summary>
    Pins a hotkey's hold time defaulting to zero, meaning the tap it has
    always been.
    </summary>
    <remarks>
    The hold time was added later, and every configuration written before it
    existed has no such field. Defaulting to zero is what keeps those files
    behaving exactly as they did, since anything else would change the
    behaviour of keys the user never touched. This is a timed press within
    one action, quite separate from the hold action, which stays down until
    toggled off.
    </remarks>
    """
    base = "[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 0\ncolumn = 0\naction = "
    plain = cfg.parse(base + "{ type = 'hotkey', keys = 'y' }", "/base").pages[0].keys[(0, 0)].action
    assert plain.params == {"keys": "y", "hold_ms": 0}  # a tap, as it always was
    held = cfg.parse(base + "{ type = 'hotkey', keys = 'y', hold_ms = 1500 }", "/base").pages[0].keys[(0, 0)].action
    assert held.params["hold_ms"] == 1500


def test_boost_and_repeat_parse_with_defaults():
    """<summary>
    Pins the two newer toggling actions parsing from their minimum form with
    their defaults filled in.
    </summary>
    <remarks>
    A boost keeps its held key and its pulsed key as separate fields, which
    is the distinction the whole action rests on. As elsewhere, the defaults
    are spread from the shared constants so this cannot drift into asserting
    stale numbers.
    </remarks>
    """
    text = """
[[pages]]
name = "A"
[[pages.keys]]
row = 0
column = 0
action = { type = "boost", hold = "w", keys = "shift" }
[[pages.keys]]
row = 0
column = 1
action = { type = "repeat", keys = "f" }
"""
    keys = cfg.parse(text, "/base").pages[0].keys
    assert keys[(0, 0)].action.params == {"hold": "w", "keys": "shift", **cfg.BOOST_DEFAULTS}
    assert keys[(0, 1)].action.params == {"keys": "f", "every_ms": cfg.REPEAT_DEFAULT_EVERY_MS}


@pytest.mark.parametrize("action, message", [
    ("{ type = 'boost', hold = 'w', keys = 'shift', on_ms = 0 }", "on_ms must be above zero"),
    ("{ type = 'repeat', keys = 'f', every_ms = 0 }", "every_ms must be above zero"),
    ("{ type = 'boost', hold = 'banana', keys = 'shift' }", "unknown key"),
    ("{ type = 'multi', steps = [ { type = 'boost', hold = 'w', keys = 'shift' } ] }", "cannot be a step"),
    ("{ type = 'multi', steps = [ { type = 'repeat', keys = 'f' } ] }", "cannot be a step"),
])
def test_bad_toggling_actions_are_reported_at_load(action, message):
    """<summary>
    Pins the same refusals for boost and repeat that the older toggling
    actions already get.
    </summary>
    <remarks>
    A period of zero is the one that bites: a loop told to wait no time
    between pulses spins a thread at full speed, typing as fast as the
    backend will accept, and the only way to stop it is to press the deck
    key again. The unknown key check applies to the held key as well as the
    pulsed one, which is easy to miss because the held key is not the field
    named "keys".

    Both are refused inside a multi for the same reason a hold is: they stay
    on until something switches them off, and a multi runs once and forgets
    them, so nothing would ever release them.
    </remarks>
    """
    text = f"[[pages]]\nname = 'A'\n[[pages.keys]]\nrow = 0\ncolumn = 0\naction = {action}"
    with pytest.raises(cfg.ConfigError, match=message):
        cfg.parse(text, "/base")


def test_text_action_parses_with_defaults_and_may_be_empty():
    """<summary>
    Pins a text action keeping its text as written, defaulting enter off and
    the delay to zero, and loading with no text at all.
    </summary>
    <remarks>
    The text is kept whole, spaces and newlines included, because the
    whitespace stripping that every other text field gets would eat a
    deliberate trailing space or a line break the user typed. Empty text has
    to load for the same reason an empty key list does: the page saves the
    key the moment its type is chosen.
    </remarks>
    """
    text = """
[[pages]]
name = "A"
[[pages.keys]]
row = 0
column = 0
action = { type = "text", text = "hello world \\n", enter = true, delay_ms = 40 }
[[pages.keys]]
row = 0
column = 1
action = { type = "text" }
"""
    keys = cfg.parse(text, "/base").pages[0].keys
    assert keys[(0, 0)].action.params == {"text": "hello world \n", "enter": True, "delay_ms": 40}
    assert keys[(0, 1)].action.params == {"text": "", "enter": False, "delay_ms": 0}


def test_request_action_parses_with_defaults():
    """<summary>
    Pins a request action's defaults (GET, ten seconds, nothing else) and
    that every optional part is kept when given, with the method upper cased.
    </summary>
    <remarks>
    The absent parts must be absent from the params rather than present as
    None or empty, because the document form writes the params out as they
    stand and TOML has no way to write None. The method is upper cased so a
    hand written "post" means the same as "POST" and the runner never has to
    care.
    </remarks>
    """
    text = """
[[pages]]
name = "A"
[[pages.keys]]
row = 0
column = 0
action = { type = "request", url = "https://example.org/hook" }
[[pages.keys]]
row = 0
column = 1
action = { type = "request", url = "https://example.org/api", method = "post", body = '{"a": 1}', headers = { X-Test = "1" }, token_file = "~/tokens/ha", timeout_s = 30 }
"""
    keys = cfg.parse(text, "/base").pages[0].keys
    assert keys[(0, 0)].action.params == {"url": "https://example.org/hook", "method": "GET", "timeout_s": cfg.REQUEST_DEFAULT_TIMEOUT_S}
    assert keys[(0, 1)].action.params == {
        "url": "https://example.org/api", "method": "POST", "body": '{"a": 1}',
        "headers": {"X-Test": "1"}, "token_file": "~/tokens/ha", "timeout_s": 30,
    }


@pytest.mark.parametrize("action, message", [
    ("{ type = 'text', text = 5 }", "'text' must be text"),
    ("{ type = 'text', text = 'a', enter = 'yes' }", "true or false"),
    ("{ type = 'text', text = 'a', delay_ms = 5000 }", "between 0 and 1000"),
    ("{ type = 'request' }", "'url' is required"),
    ("{ type = 'request', url = 'https://example.org', method = 'FETCH' }", "'method' must be one of"),
    ("{ type = 'request', url = 'https://example.org', body = 5 }", "'body' must be text"),
    ("{ type = 'request', url = 'https://example.org', headers = { X = 5 } }", "table of text values"),
    ("{ type = 'request', url = 'https://example.org', token_file = 'tokens/ha' }", "absolute path"),
    ("{ type = 'request', url = 'https://example.org', timeout_s = 0 }", "between 1 and 120"),
])
def test_bad_text_and_request_actions_are_reported_at_load(action, message):
    """<summary>
    Pins the refusals for the text and request types, each naming the field
    at fault.
    </summary>
    <remarks>
    The relative token file case is the one with a reason beyond tidiness.
    A relative path would resolve somewhere the user did not choose, most
    likely inside the config folder, which is the one place a token must not
    live because that folder is what gets copied between machines and what
    the page writes back.
    </remarks>
    """
    text = f'[[pages]]\nname = "A"\n[[pages.keys]]\nrow = 0\ncolumn = 0\naction = {action}'
    with pytest.raises(cfg.ConfigError, match=message):
        cfg.parse(text, "/base")


def test_new_action_types_parse_with_their_defaults():
    """<summary>
    Pins the shape each of the seven new types parses to: toggle halves as
    nested actions, timer seconds and done, stopwatch and counter with their
    step and reset forms, the three volume forms, the two output forms, and
    a window action's operation defaulting to focus.
    </summary>
    <remarks>
    The absent parts must be absent from params, not present as None or
    False, because the document form writes params out as they stand and
    the page then shows a value nobody set. The mute spelled as ``true`` is
    accepted as "on" because that is what a hand written file will say.
    </remarks>
    """
    text = """
[[pages]]
name = "A"
[[pages.keys]]
row = 0
column = 0
action = { type = "toggle", on = { type = "hotkey", keys = "ctrl+1" }, off = { type = "hotkey", keys = "ctrl+2" } }
[[pages.keys]]
row = 0
column = 1
action = { type = "timer", seconds = 300, done = { type = "launch", command = "paplay ding.wav" } }
action_long = { type = "timer", reset = true }
[[pages.keys]]
row = 0
column = 2
action = { type = "stopwatch" }
action_long = { type = "stopwatch", reset = true }
[[pages.keys]]
row = 0
column = 3
action = { type = "counter" }
action_long = { type = "counter", reset = true }
action_double = { type = "counter", step = -1 }
[[pages.keys]]
row = 0
column = 4
action = { type = "volume", value = 50 }
action_long = { type = "volume", delta = -5 }
action_double = { type = "volume", mute = true }
[[pages.keys]]
row = 1
column = 0
action = { type = "audio_output", device = "headphones" }
action_long = { type = "audio_output", cycle = true }
[[pages.keys]]
row = 1
column = 1
action = { type = "window", match = "firefox", command = "firefox" }
action_long = { type = "window", match = "firefox", operation = "minimise" }
[[pages.keys]]
row = 1
column = 2
action = { type = "toggle" }
"""
    keys = cfg.parse(text, "/base").pages[0].keys
    toggle = keys[(0, 0)].action
    assert toggle.type == "toggle" and toggle.params["on"].params["keys"] == "ctrl+1" and toggle.params["off"].type == "hotkey"
    timer = keys[(0, 1)]
    assert timer.action.params["seconds"] == 300 and timer.action.params["done"].type == "launch"
    assert timer.action_long.params == {"reset": True}
    assert keys[(0, 2)].action.params == {} and keys[(0, 2)].action_long.params == {"reset": True}
    counter = keys[(0, 3)]
    assert counter.action.params == {"step": 1} and counter.action_long.params == {"reset": True}
    assert counter.action_double.params == {"step": -1}
    volume = keys[(0, 4)]
    assert volume.action.params == {"value": 50} and volume.action_long.params == {"delta": -5}
    assert volume.action_double.params == {"mute": "on"}
    output = keys[(1, 0)]
    assert output.action.params == {"device": "headphones"} and output.action_long.params == {"cycle": True}
    window = keys[(1, 1)]
    assert window.action.params == {"match": "firefox", "operation": "focus", "command": "firefox"}
    assert window.action_long.params == {"match": "firefox", "operation": "minimise"}
    assert keys[(1, 2)].action.params == {}


def test_active_face_fields_parse_and_resolve():
    """<summary>
    Pins a key's active picture resolving like its ordinary one and its
    active label being kept, with both absent by default.
    </summary>
    """
    text = """
[[pages]]
name = "A"
[[pages.keys]]
row = 0
column = 0
image = "images/mic.png"
image_active = "images/mic-off.png"
label_active = "Muted"
action = { type = "volume", mute = "toggle" }
[[pages.keys]]
row = 0
column = 1
"""
    keys = cfg.parse(text, "/base").pages[0].keys
    assert keys[(0, 0)].image_active == Path("/base/images/mic-off.png")
    assert keys[(0, 0)].label_active == "Muted"
    assert keys[(0, 1)].image_active is None and keys[(0, 1)].label_active is None


@pytest.mark.parametrize("action, message", [
    ("{ type = 'multi', steps = [ { type = 'toggle' } ] }", "cannot be a step"),
    ("{ type = 'multi', steps = [ { type = 'counter' } ] }", "cannot be a step"),
    ("{ type = 'toggle', on = { type = 'hold', keys = 'w' } }", "cannot be a step"),
    ("{ type = 'toggle', on = { type = 'timer', seconds = 5 } }", "cannot be a step"),
    ("{ type = 'timer' }", "needs 'seconds'"),
    ("{ type = 'timer', seconds = 0 }", "between 1 and 86400"),
    ("{ type = 'timer', seconds = 5, done = { type = 'multi', steps = [] } }", "cannot contain"),
    ("{ type = 'counter', step = 5000 }", "between -1000 and 1000"),
    ("{ type = 'counter', reset = 'yes' }", "true or false"),
    ("{ type = 'volume' }", "exactly one of"),
    ("{ type = 'volume', value = 10, delta = 5 }", "exactly one of"),
    ("{ type = 'volume', delta = 0 }", "must not be zero"),
    ("{ type = 'volume', mute = 'sometimes' }", "'mute' must be one of"),
    ("{ type = 'audio_output' }", "needs 'device'"),
    ("{ type = 'audio_output', device = '(' }", "not a valid regular expression"),
    ("{ type = 'window' }", "'match' is required"),
    ("{ type = 'window', match = 'x', operation = 'explode' }", "'operation' must be one of"),
    ("{ type = 'toggle', on = { type = 'page', page = 'Nowhere' } }", "does not exist"),
    ("{ type = 'timer', seconds = 5, done = { type = 'page', page = 'Nowhere' } }", "does not exist"),
])
def test_bad_new_actions_are_reported_at_load(action, message):
    """<summary>
    Pins the refusals for the new types, each naming the field at fault,
    and pins the page target check reaching into a toggle's halves and a
    timer's done.
    </summary>
    <remarks>
    The nesting refusals are the ones with a reason beyond tidiness. A
    positional action inside a multi would have no key to act on, and a
    hold inside a toggle would run until pressed again with nothing to
    press: both would load fine and then do something surprising.
    </remarks>
    """
    text = f'[[pages]]\nname = "A"\n[[pages.keys]]\nrow = 0\ncolumn = 0\naction = {action}'
    with pytest.raises(cfg.ConfigError, match=message):
        cfg.parse(text, "/base")
