"""<summary>
One small door onto the desktop's session bus, for the few things the daemon
asks the desktop for: a screensaver inhibit, a notification inhibit, and what
the media players are playing.
</summary>
<remarks>
Everything here goes through Gio from PyGObject, which the Linux build already
carries for the window. The import is deferred and every failure to reach the
bus is raised as one <see cref="BusError"/>, so a machine with no session bus
(Windows, a headless box, a test) sees one kind of error and the callers can
say "not available here" and carry on.

Nothing on the bus is ever written to by this module beyond the calls the
callers name: there is no general purpose proxy and no signal handling. A
call blocks for at most <see cref="TIMEOUT_MS"/>, which keeps a stuck desktop
service from stalling a key press for long.

This module knows nothing about the deck and never imports the device layer.
</remarks>
"""

from __future__ import annotations

from typing import Any

TIMEOUT_MS = 2000

DBUS_NAME = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
PROPERTIES = "org.freedesktop.DBus.Properties"


class BusError(RuntimeError):
    """<summary>The session bus is not there, or a call on it failed.</summary>"""


def _gio():
    """<summary>Gio and GLib, imported on first use.</summary>
    <returns>The (Gio, GLib) pair.</returns>
    <exception cref="BusError">PyGObject is not installed.</exception>"""
    try:
        from gi.repository import Gio, GLib  # noqa: N813
    except Exception as err:  # gi missing, or no typelibs
        raise BusError(f"no session bus support: {err}") from err
    return Gio, GLib


def _bus():
    """<summary>The session bus connection, shared for the process.</summary>
    <returns>A Gio.DBusConnection.</returns>
    <exception cref="BusError">There is no session bus to connect to.</exception>"""
    Gio, _GLib = _gio()
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception as err:
        raise BusError(f"session bus unavailable: {err}") from err


def call(name: str, path: str, interface: str, method: str,
         signature: str | None = None, args: tuple = ()) -> tuple:
    """<summary>
    Call one method on the session bus and wait for its reply.
    </summary>
    <param name="name">The bus name to call, such as org.freedesktop.ScreenSaver.</param>
    <param name="path">The object path.</param>
    <param name="interface">The interface the method is on.</param>
    <param name="method">The method name.</param>
    <param name="signature">The GVariant type of the arguments as a tuple,
    such as "(ss)", or None for a call with no arguments.</param>
    <param name="args">The argument values, matching the signature.</param>
    <returns>The reply unpacked to plain Python values, as a tuple.</returns>
    <remarks>
    The reply is unpacked wholesale, so a method returning one value gives a
    one element tuple. Dictionaries with variant values come back as plain
    dicts, which is what the media player metadata needs.
    </remarks>
    <exception cref="BusError">The bus is unavailable or the call failed,
    which includes the service not being there.</exception>"""
    Gio, GLib = _gio()
    bus = _bus()
    try:
        packed = GLib.Variant(signature, args) if signature else None
        reply = bus.call_sync(name, path, interface, method, packed, None,
                              Gio.DBusCallFlags.NONE, TIMEOUT_MS, None)
    except Exception as err:
        raise BusError(f"{interface}.{method} failed: {err}") from err
    return tuple(reply.unpack()) if reply is not None else ()


def get_property(name: str, path: str, interface: str, prop: str) -> Any:
    """<summary>Read one property of an object on the session bus.</summary>
    <param name="name">The bus name.</param>
    <param name="path">The object path.</param>
    <param name="interface">The interface the property belongs to.</param>
    <param name="prop">The property name.</param>
    <returns>The value as plain Python.</returns>
    <exception cref="BusError">As for <see cref="call"/>.</exception>"""
    (value,) = call(name, path, PROPERTIES, "Get", "(ss)", (interface, prop))
    return value


def list_names() -> list[str]:
    """<summary>Every name currently owned on the session bus.</summary>
    <returns>The names, unique ones (org.x.y) and connection ids (:1.23) alike.</returns>
    <exception cref="BusError">As for <see cref="call"/>.</exception>"""
    (names,) = call(DBUS_NAME, DBUS_PATH, DBUS_NAME, "ListNames")
    return list(names)
