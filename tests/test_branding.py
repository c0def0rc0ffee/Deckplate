"""<summary>
The Deckplate logo: one picture on the desktop entry, the window, the tab and
the page header.
</summary>
<remarks>
The logo lives twice, as deploy/deckplate.svg for the desktop entry and as
deckplate/web/logo.svg for the page and the window, because the built program
carries the page folder and not the deploy folder. These tests keep the two
the same and keep the file readable as an icon. Nothing here opens a window
or touches a deck.
</remarks>
"""

from pathlib import Path

import pytest

from deckplate import gui

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ICON = ROOT / "deploy" / "deckplate.svg"
WEB_LOGO = ROOT / "deckplate" / "web" / "logo.svg"


def test_page_logo_is_a_copy_of_the_desktop_icon():
    """<summary>
    The page's logo is byte for byte the desktop icon, so the two cannot drift.
    </summary>
    <remarks>
    Change deploy/deckplate.svg and copy it over deckplate/web/logo.svg, then
    rerun tools/make_icon.py for the Windows .ico.
    </remarks>
    """
    assert WEB_LOGO.read_bytes() == DEPLOY_ICON.read_bytes()


def test_svg_tag_opens_before_any_comment():
    """<summary>
    The opening svg tag comes before any comment and near the top of the file.
    </summary>
    <remarks>
    GTK sniffs the start of the file for the svg tag to decide what it is. With
    the doc comment above the tag the file was not recognised at all and the
    desktop shortcut showed a blank page instead of the logo.
    </remarks>
    """
    text = DEPLOY_ICON.read_text(encoding="utf-8")
    tag = text.find("<svg")
    assert 0 <= tag < 128
    comment = text.find("<!--")
    assert comment == -1 or comment > tag


def test_icon_loads_through_gtk():
    """<summary>
    GTK's own image loader reads the icon, the same path the desktop takes.
    </summary>
    <remarks>Skipped on a machine without PyGObject or the SVG loader.</remarks>
    """
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ValueError, ImportError):
        pytest.skip("GdkPixbuf is not available")
    if not any("svg" in f.get_name() for f in GdkPixbuf.Pixbuf.get_formats()):
        pytest.skip("no SVG loader for GdkPixbuf on this machine")
    picture = GdkPixbuf.Pixbuf.new_from_file_at_size(str(DEPLOY_ICON), 48, 48)
    assert picture.get_width() == 48


def test_page_uses_the_logo_for_tab_and_header():
    """<summary>The page names the logo as its tab icon and shows it in the header.</summary>"""
    html = (ROOT / "deckplate" / "web" / "index.html").read_text(encoding="utf-8")
    assert '<link rel="icon" type="image/svg+xml" href="/static/logo.svg">' in html
    assert '<img class="logo" src="/static/logo.svg"' in html


def test_window_icon_is_the_logo():
    """<summary>The native window's icon is the page's logo file, and it exists.</summary>"""
    assert gui.WINDOW_ICON == WEB_LOGO
    assert gui.WINDOW_ICON.is_file()
