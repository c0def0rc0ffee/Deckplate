"""<summary>
The shipped icon themes and the safety of their references.
</summary>
<remarks>
Half of this file is about what an icon reference is not allowed to do. Theme
references come out of the configuration file and out of requests to the local
web page, and they are turned into paths on disk, so a reference that escapes
the themes folder is a way to read files elsewhere on the machine. The
resolver answers None for anything unsafe rather than raising, so the caller
treats it the same as a missing icon.
</remarks>
"""

from pathlib import Path

from deckplate import themes


def test_space_game_theme_is_present_with_its_icons():
    """<summary>
    The shipped theme is listed, named, and every icon it advertises has a real PNG
    behind it.
    </summary>
    <remarks>
    The listing and the files are maintained separately, so they drift: an icon
    renamed on disk but left in the listing still appears in the picker and gives a
    blank key when chosen. Walking every listed icon is what catches that, rather
    than spot checking the few named above.
    </remarks>
    """
    found = {t["slug"]: t for t in themes.list_themes()}
    assert "space-game" in found
    space = found["space-game"]
    assert space["name"] == "Space Game"
    ids = {icon["id"] for icon in space["icons"]}
    assert {"power", "landing-gear", "quantum", "hangar"} <= ids
    # every listed icon has a label and a real PNG behind it
    for icon in space["icons"]:
        assert icon["label"]
        assert themes.icon_path("space-game", icon["id"]) is not None


def test_icon_path_resolves_a_real_icon():
    """<summary>
    A valid theme and icon pair resolves to a PNG file that exists.
    </summary>
    <remarks>
    The suffix is checked as well as existence because the resolver is what the
    themes endpoint serves bytes from, and it must not hand back something that is
    not a picture.
    </remarks>
    """
    path = themes.icon_path("space-game", "power")
    assert path is not None and path.is_file() and path.suffix == ".png"


def test_icon_path_rejects_missing_and_unsafe_references():
    """<summary>
    Unknown themes, unknown icons and any reference that tries to climb out of the
    themes folder all resolve to nothing.
    </summary>
    <remarks>
    This is the traversal guard, and it is reached from the network: the local web
    page asks for icons by theme and name, so a reference escaping the folder would
    let a request read arbitrary files from the machine. Both halves of the
    reference are checked, because guarding only the icon name leaves the theme
    slug as an open door.

    The last case is the one most easily lost in a refactor. With ``must_exist``
    turned off there is no file system check to accidentally catch an unsafe path,
    so the rejection has to be explicit, and it must still return None rather than
    a path that merely does not exist yet.
    </remarks>
    """
    assert themes.icon_path("space-game", "not-an-icon") is None
    assert themes.icon_path("no-such-theme", "power") is None
    # no traversal out of the themes folder, by any spelling
    assert themes.icon_path("space-game", "../../secret") is None
    assert themes.icon_path("..", "power") is None
    assert themes.icon_path("space-game", "..") is None
    # must_exist False still refuses an unsafe reference (returns None, not a path)
    assert themes.icon_path("space-game", "../x", must_exist=False) is None


def test_parse_ref_and_ref_for_round_trip():
    """<summary>
    A theme reference parses into its two parts, and a resolved path turns back
    into the reference it came from.
    </summary>
    <remarks>
    The round trip is what lets the configuration page show a themed icon as
    themed rather than as a file path, so that saving the page back does not
    quietly rewrite every theme reference into an absolute path on this machine.
    Plain file references and malformed ones parse to None and are left alone.
    </remarks>
    """
    assert themes.parse_ref("theme:space-game/power") == ("space-game", "power")
    assert themes.parse_ref("images/power.png") is None
    assert themes.parse_ref("theme:no-slash") is None
    path = themes.icon_path("space-game", "power")
    assert themes.ref_for(path) == "theme:space-game/power"
    assert themes.ref_for(Path("nowhere/else.png")) is None
