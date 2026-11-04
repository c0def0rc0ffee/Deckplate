"""<summary>
The daemon's icon in the system tray: a menu to open the configuration
window or quit, and nothing else.
</summary>
<remarks>
The daemon runs without a terminal once it is started from the desktop, and
until this there was no way to reach it afterwards short of ``pkill``. The
icon gives it two handles: Open, which starts a separate ``deckplate gui``
process exactly as the desktop launcher does, and Quit, which sets a flag the
run loop in <see cref="cli.cmd_run"/> checks.

Nothing here talks to the deck or knows there is one. The icon is drawn in
memory, the window is another process, and the quit is a flag the daemon
reads at its own pace, so a fault in this module can cost the icon and
nothing else.

pystray is optional, like pywebview, and is imported inside
<see cref="Tray.start"/> rather than at the top, so the daemon starts on a
machine without it and simply logs that there is no icon. Linux needs a
tray to put it in: on a desktop that has one, pystray goes through the
AppIndicator typelib (``gir1.2-ayatanaappindicator3-0.1``) or falls back to
a plain X11 icon. The menu actions arrive on pystray's own thread, which is
why both of them do so little: hand off and return.
</remarks>
"""

from __future__ import annotations

import subprocess
import sys
import threading
from typing import Callable

from PIL import Image, ImageDraw

TITLE = "Deckplate"
OPEN_TEXT = "Open Deckplate"
QUIT_TEXT = "Quit Deckplate"
ICON_SIZE = 64

# The icon's colours, the same four as tools/make_icon.py and deploy/deckplate.svg.
PLATE = "#1b242e"
KEY = "#2c3a48"
SIGNAL = "#5c9bde"
STRIP = "#0f1620"
LIT = {(0, 0), (1, 0), (0, 1), (4, 2)}  # column, row of the keys drawn in blue


def draw_icon(size: int = ICON_SIZE) -> Image.Image:
    """<summary>
    Draw the Deckplate icon in memory, for the tray.
    </summary>
    <param name="size">The finished edge length in pixels.</param>
    <returns>An RGBA image, transparent outside the plate.</returns>
    <remarks>
    The same picture as ``deploy/deckplate.svg`` and the Windows ``.ico``:
    a dark plate, three rows of five keys and the display strip down the
    right, in the SVG's 64 unit grid. The geometry is a copy of the one in
    ``tools/make_icon.py``, because that file is a generator in the tools
    folder and the built program does not carry it, while Pillow cannot read
    the SVG. A test draws both at 64 pixels and compares them, so the two
    cannot drift without the suite saying so; change one, change the other.

    Drawn four times larger and shrunk, as the generator does, so the small
    tray sizes get clean edges rather than a row of aliased blocks.
    </remarks>
    """
    scale = 4
    big = size * scale
    unit = big / 64
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)

    def box(x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
        """<summary>A rectangle in unit coordinates scaled to pixels, in the order PIL wants.</summary>"""
        return (x * unit, y * unit, (x + w) * unit, (y + h) * unit)

    canvas.rounded_rectangle(box(2, 14, 60, 36), radius=6 * unit, fill=PLATE,
                             outline=SIGNAL, width=max(1, round(2 * unit)))
    for row in range(3):
        for column in range(5):
            colour = SIGNAL if (column, row) in LIT else KEY
            canvas.rounded_rectangle(box(8 + column * 8, 21 + row * 8, 6, 6), radius=unit, fill=colour)
        canvas.rounded_rectangle(box(49, 21 + row * 8, 6, 6), radius=unit, fill=STRIP,
                                 outline=SIGNAL, width=max(1, round(unit)))
    return image.resize((size, size), Image.LANCZOS)


def gui_command() -> list[str]:
    """<summary>
    The command line that opens the configuration window from this program.
    </summary>
    <returns>An argument list for subprocess, program first.</returns>
    <remarks>
    From source the program is the interpreter running the package, so the
    window is ``python -m deckplate gui`` on the same interpreter and the same
    environment. In the single file build ``sys.executable`` is the built
    program itself, which takes the subcommand directly. Either way it is the
    same ``gui`` command the desktop launcher runs, so the window the tray
    opens is the window a click on the icon opens.
    </remarks>
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "gui"]
    return [sys.executable, "-m", "deckplate", "gui"]


def open_page(popen: Callable = subprocess.Popen, log: Callable[[str], None] = print) -> bool:
    """<summary>
    Start the configuration window as a separate process and return at once.
    </summary>
    <param name="popen">Called like subprocess.Popen; replaceable in tests.</param>
    <param name="log">Where the launch line and any failure go.</param>
    <returns>True when the process was started, False when it could not be.</returns>
    <remarks>
    A separate process, not a window in this one, for the reason gui.py
    gives: pywebview runs its native loop on the calling thread and only
    returns when the window closes, and this process's main thread is
    driving the deck. The daemon stays up when the window closes, which is
    what the desktop launcher does too.

    Nothing is inherited: the three standard streams go to nowhere so a
    daemon started under systemd does not hand its journal to the window,
    and on Windows the child gets no console of its own, since the built
    program is a console program and a black window behind every open would
    be the price otherwise. The child is not waited for and its exit code is
    not read; whether the window opened is the window's own business.
    </remarks>
    """
    command = gui_command()
    options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        options["start_new_session"] = True
    try:
        popen(command, **options)
    except OSError as err:
        log(f"could not open the configuration window ({err})")
        return False
    log("configuration window opened from the tray")
    return True


class Tray:
    """<summary>
    The tray icon's life: shown, hidden, and following the config.
    </summary>
    <remarks>
    One of these lives for the whole run. <see cref="follow"/> is what the
    daemon calls, at start and on every config reload, and it starts or
    stops the icon to match ``[tray] enabled``, so the tick on the
    configuration page takes effect at the next save without a restart.

    Every failure is logged once and swallowed. The icon is a convenience
    and the deck must not stop for want of it: a machine with no tray, no
    pystray or no display gets a line in the log and nothing else. The
    "unavailable" flag keeps a daemon whose config reloads often from
    writing that line every time.

    Thread safety: start and stop take a lock because a reload on the deck
    loop and a quit from the icon's thread can arrive together.
    </remarks>
    """

    def __init__(self, open_page: Callable[[], object], quit: Callable[[], object],
                 log: Callable[[str], None] = print) -> None:
        """<summary>Build over the two things the menu does, without showing anything.</summary>
        <param name="open_page">Called on Open and on a click on the icon.</param>
        <param name="quit">Called on Quit. Must return quickly: it runs on the
        icon's thread, so it should set a flag rather than stop anything.</param>
        <param name="log">Where notices go.</param>"""
        self._open = open_page
        self._quit = quit
        self.log = log
        self._icon = None
        self._lock = threading.Lock()
        self._unavailable = False

    @property
    def running(self) -> bool:
        """<summary>True while an icon is in the tray.</summary>"""
        return self._icon is not None

    def start(self) -> bool:
        """<summary>
        Put the icon in the tray, if it is not there already.
        </summary>
        <returns>True when an icon is showing afterwards, False when the
        machine cannot show one.</returns>
        <remarks>
        pystray is imported here, inside, so a machine without it pays
        nothing until the icon is asked for and then gets one clear log
        line. Its own loop is run detached on the thread it chooses; a
        backend that has no detached mode says so with NotImplementedError
        and is given a daemon thread instead, which is what the X11 one
        does for itself anyway. Daemon, so a tray loop that will not stop
        cannot keep the process alive after the deck loop has ended.
        </remarks>
        """
        with self._lock:
            if self._icon is not None:
                return True
            try:
                icon = self._make_icon()
            except Exception as err:  # no pystray, no backend, no display
                if not self._unavailable:
                    self.log(f"no tray icon: {err}")
                    self._unavailable = True
                return False
            try:
                icon.run_detached()
            except NotImplementedError:
                threading.Thread(target=icon.run, name="deckplate-tray", daemon=True).start()
            except Exception as err:
                if not self._unavailable:
                    self.log(f"no tray icon: {err}")
                    self._unavailable = True
                return False
            self._icon = icon
        self.log("tray icon shown")
        return True

    def stop(self) -> None:
        """<summary>
        Take the icon out of the tray, if it is there. Safe to call twice.
        </summary>
        <remarks>
        Hidden before it is stopped, because the GTK backends leave a ghost
        in the tray when a visible icon is stopped outright. A backend that
        objects is logged and the icon is forgotten regardless, so the
        daemon is never stuck on a tray it cannot clear.
        </remarks>
        """
        with self._lock:
            icon, self._icon = self._icon, None
        if icon is None:
            return
        try:
            icon.visible = False
            icon.stop()
        except Exception as err:
            self.log(f"tray icon not removed cleanly ({err})")
            return
        self.log("tray icon removed")

    def follow(self, config) -> None:
        """<summary>
        Show or remove the icon as the config says.
        </summary>
        <param name="config">A loaded Config; only ``tray.enabled`` is read.</param>
        <remarks>Passed to the controller as its reload hook, so this is
        called on the deck loop after every reload as well as once from the
        daemon at start. Both directions are idempotent.</remarks>
        """
        if config.tray.enabled:
            self.start()
        else:
            self.stop()

    def _on_open(self, *_args) -> None:
        """<summary>The Open item and the icon's own click.</summary>
        <remarks>pystray calls actions with the icon and the item, or with
        nothing, depending on the signature it finds; taking anything keeps
        it from mattering.</remarks>"""
        self._open()

    def _on_quit(self, *_args) -> None:
        """<summary>The Quit item.</summary>"""
        self._quit()

    def _make_icon(self):
        """<summary>Build the pystray icon with its two item menu, not yet running.</summary>
        <returns>A pystray Icon.</returns>
        <exception cref="ImportError">pystray is not installed, or has no
        backend for this machine.</exception>
        <remarks>The Open item is the default, which is what a left click or a
        double click on the icon runs where the platform has such a thing.</remarks>"""
        import pystray  # optional dependency, see the module header

        menu = pystray.Menu(
            pystray.MenuItem(OPEN_TEXT, self._on_open, default=True),
            pystray.MenuItem(QUIT_TEXT, self._on_quit),
        )
        return pystray.Icon("deckplate", draw_icon(), TITLE, menu)
