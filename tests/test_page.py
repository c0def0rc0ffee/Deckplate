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
