"""<summary>
The configuration page's script and markup, checked without a browser.
</summary>
<remarks>
The page is plain JavaScript served from a folder, with no build step, so a
syntax error in app.js stops the whole page silently: nothing catches it
before the browser does. Node's parser is the one check available on the
build host, and it is skipped, not failed, on a machine without node. The
rest pins the parts of the page that the right click move and copy depend
on, by their text, since the page itself cannot run here.
</remarks>
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "deckplate" / "web"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_app_js_parses():
    """<summary>The page script is syntactically whole, by node's parser.</summary>
    <remarks>A parse error anywhere in app.js leaves the page dead with no
    message, so this is the one test that catches a slipped bracket before a
    browser does.</remarks>"""
    subprocess.run(["node", "--check", str(WEB / "app.js")], check=True)


def test_key_cells_offer_a_right_click_menu():
    """<summary>A key cell listens for the right button and the hint says so.</summary>
    <remarks>The menu moves or copies a key to another page. The listener,
    the two menu items and the one edit that carries them out are named here
    so a rename on one side cannot quietly orphan the other.</remarks>"""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'cell.addEventListener("contextmenu"' in js
    assert 'openTransfer("move", row, column)' in js
    assert 'openTransfer("copy", row, column)' in js
    assert "function transferKey(mode, from, to, how)" in js
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "right click to move or copy it to another page" in html


def test_menu_and_picker_are_styled():
    """<summary>The stylesheet carries the classes the menu and the position picker are built with.</summary>
    <remarks>app.js builds these by name and nothing checks the two files
    agree, so a class dropped from the stylesheet would leave an unstyled
    menu floating at the top left of the page.</remarks>"""
    css = (WEB / "app.css").read_text(encoding="utf-8")
    for selector in (".menu {", ".menu-item {", ".slot-grid {", ".slot {", ".slot.free", ".slot.same"):
        assert selector in css, selector


def test_the_side_panes_have_their_toggles_and_their_slide():
    """<summary>The pages list and the editor each sit in a column the stylesheet
    slides shut, with a toggle in the header for each.</summary>
    <remarks>The ids are app.js's contract with the markup and the classes
    are the stylesheet's; a rename on any side leaves a pane that cannot be
    opened or one that snaps instead of sliding, with nothing thrown.</remarks>"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for marker in ('id="pagesBtn"', 'id="editorBtn"', 'id="pagesCol"', 'id="editorCol"', 'id="pagesPane"',
                   'id="pagesSearch"', 'id="pagesSort"', 'id="pageMenu"', 'id="pagesAdd"',
                   '<main class="pages-closed booting">', 'value="edited"', 'value="shown"'):
        assert marker in html, marker
    css = (WEB / "app.css").read_text(encoding="utf-8")
    for selector in ("--swoosh:", "transition: width var(--swoosh)", "main.pages-closed .pages-col",
                     "main.editor-closed .editor-col", "transform: translateX(-100%)", "transform: translateX(100%)",
                     "prefers-reduced-motion", "main.booting", "button.toggle.attention::after",
                     ".page-entry {", ".page-key {", ".page-menu mark"):
        assert selector in css, selector
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for marker in ('function setPane(name, open)', 'setPane("pages", !panes.pages)', 'setPane("editor", !panes.editor)',
                   "function renderPageMenu()", "function sortedPages()", "function stampChanged(before)",
                   "function goToKey(pageName, row, column)", 'remembered.get("pane.editor", true)',
                   'toLocaleString("en-GB"'):
        assert marker in js, marker


def test_the_settings_window_offers_the_tray_tick():
    """<summary>The tray icon is turned on and off from Settings and written as tray.enabled.</summary>"""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'el("h3", { text: "Tray icon" })' in js
    assert "d.tray.enabled = tray.checked" in js
