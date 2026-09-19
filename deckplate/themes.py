"""<summary>
Built in icon themes shipped with the app.
</summary>
<remarks>
A theme is a folder under ``deckplate/themes/<slug>/`` holding a ``manifest.json``
and one ``<id>.png`` per icon. A key refers to a theme icon as the string
``theme:<slug>/<id>``. The config resolves that to the packaged PNG so it draws
on the deck, and the picker offers the set from a gallery, kept apart from the
user's own uploads.

Pure and read only: nothing here writes, and every slug and icon id from outside
is checked so a reference can never reach a file outside the themes folder.

That checking is the point of the module rather than a detail of it. A theme
reference arrives from a config file or from the configuration page, which
means it is untrusted text, and a reference built to climb out of the themes
folder would otherwise hand back any readable file on the machine to be drawn
on a key. Both the segment check and the resolved path check are needed: the
first stops a separator being smuggled in, the second catches a symlink inside
the folder pointing out of it.

A missing or unreadable theme is never an error here. A bad manifest, a folder
with no icons and an icon whose PNG has gone are all skipped quietly, because
one broken theme must not stop the other themes being offered.
</remarks>
"""

from __future__ import annotations

import json
from pathlib import Path

THEMES_DIR = Path(__file__).resolve().parent / "themes"
REF_PREFIX = "theme:"


def _segment_ok(segment: str) -> bool:
    """<summary>
    A single safe path segment: no separators, no traversal, no tricks.
    </summary>
    <param name="segment">One slug or icon id, straight from a reference.</param>
    <returns>True when the segment names nothing but a child of one folder.</returns>
    <remarks>
    Backslash is rejected as well as forward slash, because on Windows it is
    a separator too and a check that only looked for "/" would let a reference
    through. The empty string and both dot forms are rejected so a reference
    can never name the themes folder itself or its parent.
    </remarks>
    """
    return (bool(segment) and segment not in (".", "..")
            and "/" not in segment and "\\" not in segment and "\x00" not in segment)


def list_themes() -> list[dict]:
    """<summary>
    Every theme with its icons: ``[{slug, name, icons: [{id, label}]}]``.
    </summary>
    <returns>One entry per usable theme, sorted by folder name, possibly empty.</returns>
    <remarks>
    Icons whose PNG is missing are dropped, so the picker never offers a broken one,
    and a theme left with no icons at all is dropped with them rather than shown
    as an empty gallery. A theme whose manifest will not parse is skipped without
    complaint, which means a typo in one manifest shows up as a theme quietly
    absent from the picker rather than as an error to read.

    This touches the disk on every call and does no caching. Call it once and
    hold the answer rather than once per key being drawn.
    </remarks>
    """
    themes: list[dict] = []
    if not THEMES_DIR.is_dir():
        return themes
    for folder in sorted(THEMES_DIR.iterdir()):
        manifest = folder / "manifest.json"
        if not (folder.is_dir() and manifest.is_file()):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        icons = [{"id": entry["id"], "label": entry.get("label", entry["id"])}
                 for entry in data.get("icons", [])
                 if isinstance(entry, dict) and "id" in entry
                 and (folder / f"{entry['id']}.png").is_file()]
        if icons:
            themes.append({"slug": folder.name, "name": data.get("name", folder.name), "icons": icons})
    return themes


def icon_path(slug: str, icon: str, must_exist: bool = True) -> Path | None:
    """<summary>
    The PNG for one theme icon, or None if the reference is unsafe or absent.
    </summary>
    <param name="slug">Theme folder name, from a reference and so untrusted.</param>
    <param name="icon">Icon id without the .png, also untrusted.</param>
    <param name="must_exist">False to return the intended path even when the
    file is not there.</param>
    <returns>A path inside the themes folder, or None.</returns>
    <remarks>
    With ``must_exist`` False the intended path is returned even when the file is
    not there, so a stale reference draws the missing marker rather than nothing.
    That is the only reason the flag exists, and it never relaxes the safety
    checks: an unsafe reference still answers None whichever way the flag is set.

    None means do not open it, and it covers both an unsafe reference and a
    file that is simply absent. The caller cannot tell the two apart, which is
    deliberate, since the answer is the same either way.
    </remarks>
    """
    if not (_segment_ok(slug) and _segment_ok(icon)):
        return None
    path = THEMES_DIR / slug / f"{icon}.png"
    try:
        path.resolve().relative_to(THEMES_DIR.resolve())
    except (ValueError, OSError):
        return None
    if must_exist and not path.is_file():
        return None
    return path


def parse_ref(value: str) -> tuple[str, str] | None:
    """<summary>
    Split ``theme:<slug>/<id>`` into ``(slug, id)``, or None if it is not one.
    </summary>
    <param name="value">Any image value from the config, theme reference or not.</param>
    <returns>The slug and icon id, or None for an ordinary file path.</returns>
    <remarks>
    None is the ordinary answer, not a fault: most image values are plain paths
    and this is how the config tells the two apart. The split is deliberately
    the first slash only, and the halves are not checked here, so a reference
    that parses is still not a reference that is safe. Everything that comes
    out of this goes through <see cref="icon_path"/> before it reaches the disk.
    </remarks>
    """
    if not value.startswith(REF_PREFIX):
        return None
    slug, sep, icon = value[len(REF_PREFIX):].partition("/")
    if not sep:
        return None
    return slug, icon


def ref_for(path: Path) -> str | None:
    """<summary>
    If ``path`` is a theme icon file, its ``theme:<slug>/<id>`` reference.
    </summary>
    <param name="path">A resolved or unresolved path to an image.</param>
    <returns>The reference, or None when the path is not a packaged icon.</returns>
    <remarks>
    The reverse of <see cref="parse_ref"/> plus <see cref="icon_path"/>, and the
    reason a saved config keeps ``theme:`` references instead of absolute paths
    into the installation folder. Without it, saving a config on one machine
    would bake that machine's install path into the file and the icons would
    vanish anywhere else.

    Only a file sitting directly in a theme folder qualifies. Anything deeper,
    anything outside the themes folder, and anything that is not a .png answers
    None and is then written as an ordinary path.
    </remarks>
    """
    try:
        rel = path.resolve().relative_to(THEMES_DIR.resolve())
    except (ValueError, OSError):
        return None
    if len(rel.parts) == 2 and rel.parts[1].endswith(".png"):
        return f"{REF_PREFIX}{rel.parts[0]}/{rel.parts[1][:-4]}"
    return None
