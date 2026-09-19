"""<summary>
The document form of the config and the TOML emitter.
</summary>
<remarks>
This file guards one loop: a validated config goes out as a plain document,
the document is written as TOML, and the TOML is read back. The
configuration page saves by walking exactly that loop, so anything the loop
drops is silently deleted from the user's own file the first time they press
save on an unrelated key. That is the failure every test here is shaped
around, and it is why so many of them assert equality against the input
rather than against a list of fields: a field nobody remembered to check is
precisely the field that gets lost.

The TOML is written by hand rather than by a library, so the awkward text
and rejection tests are about the emitter itself and not about anything the
user would normally type.

``Path("/base")`` is the base directory relative image paths are resolved
against. It is a fixed made up root, never touched on disk, so the tests
give the same answer on any machine.
</remarks>
"""

from pathlib import Path

import pytest

from deckplate import config as cfg

TEXT = '''
[deck]
brightness = 55
sleep_after_minutes = 0
official_software = "keep"

[server]
port = 9000

[weather]
latitude = 49.45
longitude = -2.54
units = "imperial"
location_name = "St Peter Port"

[strip]
tiles = ["clock", "images/logo.png", "blank"]

[[pages]]
name = "Main"

[[pages.keys]]
row = 0
column = 4
image = "images/web.png"
label = "Web \\"quoted\\" \\\\ back"
action = { type = "url", url = "https://example.org/?a=1&b=2" }

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

[[pages.keys]]
row = 1
column = 0
action = { type = "sequence", keys = ["ctrl+l", "h", "enter"], delay_ms = 80 }
'''


def test_hold_action_round_trips_through_the_document():
    """<summary>
    Pins a hold action keeping all four of its timing values across the
    whole loop.
    </summary>
    <remarks>
    The four numbers are the only thing that distinguishes one hold from
    another, and two of them here are zero, which is the value most likely
    to be dropped by an emitter that skips anything falsy. A zero release
    range means a plain unbroken hold, so losing it turns a steady hold into
    one that keeps letting go, which in a game is the difference between
    holding a throttle and tapping it.
    </remarks>
    """
    doc = {"pages": [{"name": "A", "keys": [{"row": 0, "column": 0, "action": {
        "type": "hold", "keys": "w", "hold_min_ms": 1000, "hold_max_ms": 2000,
        "release_min_ms": 0, "release_max_ms": 0}}]}]}
    c = cfg.parse(cfg.to_toml(doc), Path("/base"))
    assert cfg.document_from_config(c)["pages"][0]["keys"][0]["action"] == doc["pages"][0]["keys"][0]["action"]


def test_backgrounds_round_trip():
    """<summary>
    Pins background colours surviving at all three levels, and pins an unset
    one coming back as None rather than as a colour.
    </summary>
    <remarks>
    The last assertion is the one with teeth. A key with no background of
    its own must come back with None, meaning it inherits, and not with the
    deck wide colour filled in for it. If the inherited value were written
    out as if it were the key's own, changing the deck background afterwards
    would leave every previously saved key stuck on the old colour, and no
    amount of editing the deck setting would move them.

    The strip is compared as a whole list so that the mixture of shapes is
    covered together: a bare string panel, a panel given as a table with a
    colour, and one with no colour at all.
    </remarks>
    """
    doc = {"deck": {"background": "#102030"},
           "strip": [{"kind": "clock", "background": "#000000"}, {"image": "images/a.png", "fit": False, "background": "#abcdef"}, "weather"],
           "pages": [{"name": "A", "keys": [{"row": 0, "column": 0, "label": "x", "background": "#ff8800"},
                                            {"row": 0, "column": 1, "label": "plain"}]}]}
    c = cfg.parse(cfg.to_toml(doc), Path("/base"))
    again = cfg.document_from_config(c)
    assert again["deck"]["background"] == "#102030"
    assert again["strip"] == doc["strip"]
    assert again["pages"][0]["keys"][0]["background"] == "#ff8800"
    assert again["pages"][0]["keys"][1]["background"] is None


def test_animations_round_trip():
    """<summary>
    Pins an animation keeping its kind, colour, speed and rate on both a key
    and a strip panel, and pins a false boolean surviving.
    </summary>
    <remarks>
    ``press_flash`` set to False is checked twice on purpose, once on the
    parsed config and once on the document. A boolean that is false is the
    classic casualty of an emitter that writes only truthy values, and the
    symptom is a setting the user switches off that switches itself back on
    at the next save, which looks like the page ignoring them.

    The speed of 2.0 matters for a different reason: it must stay a floating
    point number through the TOML, because a value written as a bare 2 comes
    back an integer and the comparison then fails on type.
    </remarks>
    """
    doc = {"deck": {"press_flash": False},
           "strip": ["clock", {"animation": {"kind": "wave", "colour": "#ffffff", "speed": 2.0, "fps": 20}}, "blank"],
           "pages": [{"name": "A", "keys": [{"row": 0, "column": 0, "label": "Go",
                      "animation": {"kind": "pulse", "colour": "#4caf7d", "speed": 1.0, "fps": 20}}]}]}
    c = cfg.parse(cfg.to_toml(doc), Path("/base"))
    assert c.deck.press_flash is False
    again = cfg.document_from_config(c)
    assert again["strip"][1] == doc["strip"][1]
    assert again["pages"][0]["keys"][0]["animation"] == doc["pages"][0]["keys"][0]["animation"]
    assert again["deck"]["press_flash"] is False


def test_strip_visible_and_fit_round_trip():
    """<summary>
    Pins the measured strip window and the per panel fit flag, and pins an
    image path being absolute in the config but relative in the document.
    </summary>
    <remarks>
    The path is the interesting part. Inside a config an image is resolved
    against the base directory, because the renderer has to open it. In the
    document it must go back to the relative form it was written in, or
    every save would bake this machine's directory layout into the user's
    file and the whole configuration would stop working the moment it was
    copied to the other machine.

    The visible window is a measured physical size rather than a preference,
    so losing it means every strip panel is drawn to the wrong window and
    the art is cropped by the bezel.
    </remarks>
    """
    doc = {"strip_visible": {"width": 84, "height": 78},
           "strip": ["clock", {"image": "images/a.png", "fit": True}, "weather"],
           "pages": [{"name": "A", "keys": []}]}
    c = cfg.parse(cfg.to_toml(doc), Path("/base"))
    assert c.strip_visible == (84, 78)
    assert c.strip[1].fit is True and c.strip[1].image == Path("/base/images/a.png")
    again = cfg.document_from_config(c)
    assert again["strip_visible"] == {"width": 84, "height": 78}
    assert again["strip"][1] == {"image": "images/a.png", "fit": True}


def test_chord_round_trips_through_the_document():
    """<summary>
    Pins a chord keeping its held key, its list of taps in order, and its
    two delay bounds.
    </summary>
    <remarks>
    The tap list must come back a list, in the same order, since a chord is
    defined by its sequence. The held key is a separate field from the taps
    and is easy to fold into them by mistake, which would turn a modifier
    held across the whole run into one more tap.
    </remarks>
    """
    doc = {"pages": [{"name": "A", "keys": [{"row": 0, "column": 0, "action": {
        "type": "chord", "hold": "alt", "keys": ["1", "2"], "delay_min_ms": 50, "delay_max_ms": 200}}]}]}
    c = cfg.parse(cfg.to_toml(doc), Path("/base"))
    assert cfg.document_from_config(c)["pages"][0]["keys"][0]["action"] == doc["pages"][0]["keys"][0]["action"]


def test_sequence_keys_come_out_as_a_list():
    """<summary>
    Pins a sequence's keys coming out of the document as a list, not as the
    tuple the config holds internally.
    </summary>
    <remarks>
    The config stores them as a tuple so a config is hashable and cannot be
    edited in place. The document is serialised as JSON for the
    configuration page, and a tuple has no JSON form, so the conversion has
    to happen on the way out. Missing it does not fail here: it fails later,
    in the web layer, as a serialisation error on save with the user's edits
    already in hand.
    </remarks>
    """
    doc = cfg.document_from_config(cfg.parse(TEXT, Path("/base")))
    assert doc["pages"][1]["keys"][1]["action"] == {"type": "sequence", "keys": ["ctrl+l", "h", "enter"], "delay_ms": 80}


def test_document_shape():
    """<summary>
    Pins the full document the configuration page receives, including every
    default filled in for a setting the file never mentioned.
    </summary>
    <remarks>
    The deck table is compared as a whole rather than field by field, which
    is deliberate: adding a setting to the deck without adding it here fails
    this test, and that is the intended warning. Every field the page draws
    a control for must be present with a value, because a missing key
    becomes an undefined control in the page and then writes itself back as
    a null on save.

    The source text sets only three of these, so the rest prove the defaults
    are materialised rather than omitted. The server token being explicitly
    None is part of that: the page needs the field to exist in order to show
    an empty box. Note that the token is only ever absent or present here;
    no real token is written into a test file.
    </remarks>
    """
    c = cfg.parse(TEXT, Path("/base"))
    doc = cfg.document_from_config(c)
    assert doc["deck"] == {"brightness": 55, "sleep_after_minutes": 0, "official_software": "keep",
                           "press_flash": True, "background": cfg.DEFAULT_BACKGROUND,
                           "long_press_ms": cfg.LONG_PRESS_DEFAULT_MS,
                           "double_press_ms": cfg.DOUBLE_PRESS_DEFAULT_MS,
                           "follow_focus": False, "focus_poll_ms": cfg.FOCUS_POLL_DEFAULT_MS,
                           "key_pitch_x": None, "key_pitch_y": None}
    assert doc["server"]["port"] == 9000 and doc["server"]["token"] is None
    assert doc["weather"]["location_name"] == "St Peter Port"
    assert doc["strip"] == ["clock", {"image": "images/logo.png", "fit": False}, "blank"]
    assert doc["strip_visible"] == {"width": 78, "height": 78}
    web = doc["pages"][0]["keys"][0]
    assert web["image"] == "images/web.png" and web["action"] == {"type": "url", "url": "https://example.org/?a=1&b=2"}
    multi = doc["pages"][0]["keys"][1]["action"]
    assert multi["type"] == "multi" and multi["delay_ms"] == 50
    assert multi["steps"][1] == {"type": "launch", "command": "gedit", "command_windows": "notepad.exe"}
    assert doc["pages"][1]["keys"][0]["action"] == {"type": "page", "page": "previous"}


def test_toml_round_trip_is_lossless():
    """<summary>
    Pins the whole loop as lossless in both senses: the config comes back
    identical, and writing it a second time produces byte for byte the same
    text.
    </summary>
    <remarks>
    The first half is the guarantee the configuration page depends on. The
    second half is what makes the file usable under version control and
    diffable by hand: an emitter whose key order or spacing wandered would
    show the user a large diff every time they changed one key, and would
    make a genuine change impossible to spot.

    The comparison is done section by section so a failure names which part
    of the configuration was lost, rather than printing the whole structure.
    The one thing the loop explicitly does not preserve is comments, since
    the document form has nowhere to put them.
    </remarks>
    """
    original = cfg.parse(TEXT, Path("/base"))
    text = cfg.to_toml(cfg.document_from_config(original))
    again = cfg.parse(text, Path("/base"))
    assert again.deck == original.deck
    assert again.server == original.server
    assert again.weather == original.weather
    assert again.strip == original.strip
    assert again.pages == original.pages
    # and the emitted text is stable
    assert cfg.to_toml(cfg.document_from_config(again)) == text


def test_absolute_image_paths_survive():
    """<summary>
    Pins an image given as an absolute path staying absolute through the
    document.
    </summary>
    <remarks>
    The counterpart to the relative case. Relative paths are resolved
    against the base directory and must come back relative, but a path the
    user wrote as absolute points somewhere outside the configuration
    directory on purpose and cannot be expressed relative to it. Rewriting
    it relative would produce a path that does not exist and a key that
    quietly loses its picture.
    </remarks>
    """
    text = '[[pages]]\nname = "A"\n[[pages.keys]]\nrow = 0\ncolumn = 0\nimage = "/somewhere/else/pic.png"\n'
    doc = cfg.document_from_config(cfg.parse(text, Path("/base")))
    assert doc["pages"][0]["keys"][0]["image"] == "/somewhere/else/pic.png"


def test_to_toml_rejects_bad_documents():
    """<summary>
    Pins the emitter refusing four kinds of document it cannot honestly
    write, with a config error rather than bad TOML.
    </summary>
    <remarks>
    The emitter is hand written, so the danger is not that it raises but
    that it happily writes something that is not valid TOML, or that parses
    as something else. Each case here is one of those. A list instead of an
    object, and a string where pages must be a list of tables, are shape
    faults. A not a number float has no TOML spelling at all. An arbitrary
    object would be written as whatever its text form happens to look like,
    which could be anything.

    Refusing at emission means the user's existing file on disk is left
    untouched and they are told, instead of it being replaced by something
    that will not load at the next start.
    </remarks>
    """
    with pytest.raises(cfg.ConfigError):
        cfg.to_toml([])
    with pytest.raises(cfg.ConfigError):
        cfg.to_toml({"pages": "nope"})
    with pytest.raises(cfg.ConfigError):
        cfg.to_toml({"pages": [{"name": "A", "keys": [{"row": float("nan")}]}]})
    with pytest.raises(cfg.ConfigError):
        cfg.to_toml({"pages": [{"name": "A", "keys": [{"row": object()}]}]})


def test_to_toml_output_parses_even_with_awkward_text():
    """<summary>
    Pins the emitter's string escaping against the characters that break a
    hand written one: tabs, quotes, backslashes and newlines.
    </summary>
    <remarks>
    Every one of these is text a user can legitimately type into a page name
    or a key label. A backslash written out unescaped changes the meaning of
    whatever follows it, and an unescaped newline ends the value early, so
    the failure is not a rejected file but a file that loads as something
    different from what was saved. Reading the text back and comparing it to
    the original is the only assertion that catches that.

    The hotkey of "ctrl++" is here because the trailing plus is easy to trim
    or to treat as an empty final key when a combination is split apart.
    </remarks>
    """
    doc = {"pages": [{"name": 'tab\there "q" \\ back', "keys": [
        {"row": 0, "column": 0, "label": "multi\nline", "action": {"type": "hotkey", "keys": "ctrl++"}}]}]}
    c = cfg.parse(cfg.to_toml(doc), Path("/base"))
    assert c.pages[0].name == 'tab\there "q" \\ back'
    assert c.pages[0].keys[(0, 0)].label == "multi\nline"


def test_document_with_bad_values_fails_validation_not_emission():
    """<summary>
    Pins where an out of range value is caught: by the parser on the way
    back in, not by the emitter on the way out.
    </summary>
    <remarks>
    This is a division of labour worth keeping. The emitter's job is only to
    write valid TOML, and validation lives in one place, the parser, so
    there is a single set of rules and a single set of messages rather than
    two that can disagree. The error message is matched, not just the type,
    because the user's only route to fixing it is being told the range: the
    deck has three rows, so a row of nine is a mistake that must be named
    rather than clamped.
    </remarks>
    """
    doc = {"pages": [{"name": "A", "keys": [{"row": 9, "column": 0}]}]}
    text = cfg.to_toml(doc)
    with pytest.raises(cfg.ConfigError, match="between 0 and 2"):
        cfg.parse(text, Path("/base"))


def test_text_and_request_actions_round_trip_through_the_document():
    """<summary>
    Pins both new types surviving the save loop whole: the text's enter flag
    and delay, and the request's method, body, header table, token file
    path and timeout.
    </summary>
    <remarks>
    The header table is the part with something to lose. It is the only
    action parameter that is itself a table, so it is the first to find out
    whether the emitter writes a nested inline table and whether the
    document keeps it as a dict rather than flattening it. The token file
    must come back as the text that was written, never as a resolved path,
    so a config saved on one machine still names the same file on the other.
    </remarks>
    """
    actions = [
        {"type": "text", "text": "hello\nworld", "enter": True, "delay_ms": 20},
        {"type": "request", "url": "https://example.org/api", "method": "POST", "body": '{"a": 1}',
         "headers": {"X-Test": "1", "Content-Type": "text/plain"}, "token_file": "~/tokens/ha", "timeout_s": 30},
    ]
    doc = {"pages": [{"name": "A", "keys": [
        {"row": 0, "column": index, "action": action} for index, action in enumerate(actions)]}]}
    c = cfg.parse(cfg.to_toml(doc), Path("/base"))
    back = cfg.document_from_config(c)["pages"][0]["keys"]
    assert [key["action"] for key in back] == actions


def test_new_action_types_and_active_face_round_trip_through_the_document():
    """<summary>
    Pins the seven new types and the active face fields surviving the save
    loop whole, nested halves included.
    </summary>
    <remarks>
    A toggle's on and off and a timer's done are nested actions, the first
    outside a multi's steps, so this is where the document converter's
    recursion into them is proven. Losing one would silently turn a toggle
    into a key that only ever runs its on half.
    </remarks>
    """
    actions = [
        {"type": "toggle", "on": {"type": "hotkey", "keys": "ctrl+1", "hold_ms": 0},
         "off": {"type": "launch", "command": "xed"}},
        {"type": "timer", "seconds": 300, "done": {"type": "hotkey", "keys": "f5", "hold_ms": 0}},
        {"type": "stopwatch", "reset": True},
        {"type": "counter", "step": -2},
        {"type": "volume", "mute": "toggle"},
        {"type": "audio_output", "device": "head"},
        {"type": "window", "match": "code", "operation": "maximise", "command": "code"},
    ]
    doc = {"pages": [{"name": "A", "keys": [
        {"row": index // 5, "column": index % 5, "action": action, "image_active": "images/on.png", "label_active": "On"}
        for index, action in enumerate(actions)]}]}
    c = cfg.parse(cfg.to_toml(doc), Path("/base"))
    back = cfg.document_from_config(c)["pages"][0]["keys"]
    assert [key["action"] for key in back] == actions
    assert all(key["image_active"] == "images/on.png" and key["label_active"] == "On" for key in back)
