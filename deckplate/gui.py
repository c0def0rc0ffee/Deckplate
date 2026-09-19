"""<summary>
Open the configuration page in its own window.
</summary>
<remarks>
The page is served by the running daemon. This just puts a native window
around it with pywebview (WebView2 on Windows, WebKitGTK on Linux), and falls
back to the normal browser when pywebview is not installed.

Nothing here talks to the deck, or even knows there is one. It is a window
around a URL, so a fault in this module can slow the page down but can never
reach the hardware. The daemon has to already be running: this module starts
nothing, and says so plainly rather than trying to launch one, because two
daemons would fight over the same USB device.

pywebview is an optional dependency on purpose. It is imported inside the
function that needs it so that importing this module, which the command line
does for every ``gui`` run, costs nothing on a machine without it.
</remarks>
"""

from __future__ import annotations

import http.client
import sys
import webbrowser

WINDOW_TITLE = "Deckplate"
WINDOW_SIZE = (1180, 780)


def daemon_reachable(host: str, port: int) -> bool:
    """<summary>
    Ask the daemon's API whether it is there, before a window is opened on it.
    </summary>
    <param name="host">Loopback name or address the daemon binds, such as
    127.0.0.1.</param>
    <param name="port">The API port from the config file.</param>
    <returns>True when something answered as the daemon, False otherwise.</returns>
    <remarks>
    503 counts as reachable as well as 200. That is what ``/api/state``
    answers when the daemon is up but no deck is plugged in, which is a
    perfectly good reason to open the page: the page itself explains the
    missing deck far better than this function could.

    Any OSError is treated as "not there" rather than raised, because a
    refused connection is the normal answer when the daemon is simply not
    running. The two second timeout is a deliberate ceiling: a wait longer
    than that looks like a hung launcher to whoever clicked the icon.
    </remarks>
    """
    try:
        conn = http.client.HTTPConnection(host, port, timeout=2)
        conn.request("GET", "/api/state")
        status = conn.getresponse().status
        conn.close()
        return status in (200, 503)
    except OSError:
        return False


def open_window(url: str, log=print) -> int:
    """<summary>
    Put a native window around the page, or fall back to the browser.
    </summary>
    <param name="url">The daemon's page, already checked as reachable.</param>
    <param name="log">Where the fallback notices go; the command line passes
    its own timestamped printer.</param>
    <returns>Always 0. Falling back to the browser is a normal outcome, not a
    failure, so there is no exit code that means "no window".</returns>
    <remarks>
    This blocks until the window is closed: ``webview.start()`` runs the
    native event loop on the calling thread and only returns at the end. It
    must therefore be called from the main thread, which is where the command
    line calls it from.

    Two separate fallbacks, for two separate faults. pywebview missing is an
    ImportError and means the optional dependency was never installed. A
    broad Exception around ``start`` catches the other case: pywebview is
    present but the machine has no web engine behind it, which surfaces as
    assorted platform specific errors rather than one type worth naming.
    </remarks>
    """
    try:
        import webview  # pywebview
    except ImportError:
        log("pywebview is not installed (pip install pywebview); opening in the browser instead")
        webbrowser.open(url)
        return 0
    try:
        webview.create_window(WINDOW_TITLE, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                              min_size=(900, 600))
        webview.start()
    except Exception as err:  # no web engine on this machine, for example
        log(f"could not open a window ({err}); opening in the browser instead")
        webbrowser.open(url)
    return 0


def run(host: str, port: int, log=print, browser: bool = False) -> int:
    """<summary>
    The whole ``gui`` command: check the daemon, then show the page.
    </summary>
    <param name="host">Loopback name or address the daemon is bound to.</param>
    <param name="port">The API port from the config file.</param>
    <param name="log">Where notices go.</param>
    <param name="browser">True to skip the native window and hand the URL
    straight to the normal browser.</param>
    <returns>0 once the page has been shown or the window closed, 1 when
    nothing is listening.</returns>
    <remarks>
    The reachability check comes first on purpose. Opening a window on a dead
    port shows an empty frame with a browser error in it, and whoever sees
    that has no way of knowing the daemon is the missing piece, so the
    message names the command that starts one.

    Only http is built here, never https. The daemon serves plain http on
    loopback, where there is nothing between the two to protect against.
    </remarks>
    <see cref="daemon_reachable"/>
    """
    url = f"http://{host}:{port}/"
    if not daemon_reachable(host, port):
        log(f"nothing is listening at {url}. Start the daemon first:  deckplate run")
        return 1
    if browser:
        webbrowser.open(url)
        return 0
    return open_window(url, log)


if __name__ == "__main__":
    sys.exit(run("127.0.0.1", 8765))
