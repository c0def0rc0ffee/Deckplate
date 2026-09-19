"""<summary>
The USB side: a hidapi transport and the Deck class that drives the hardware.
</summary>
<remarks>
The transport is a tiny interface so the Deck can be tested with a fake that
records packets. On real hardware <see cref="HidTransport"/> wraps hidapi,
which speaks to Windows HID and Linux hidraw with the same calls.

Windows quirk seen on 7 September 2026: hidapi raised one transient read error
per session with the deck still enumerated. <see cref="Deck.read_event"/>
treats that as a signal to reopen the device and carry on rather than as fatal.

This module holds the second of the two safety gates. <see cref="Deck._send"/>
refuses to write any command outside the proven set unless the deck was opened
with ``allow_experimental``, which the daemon never does. It sits below
<see cref="protocol.command"/> on purpose, so that a packet assembled by hand
is refused just as firmly as one built through the protocol module.
</remarks>
"""

from __future__ import annotations

import time
from typing import Protocol as _Protocol

from . import images, layout, protocol
from .protocol import DeviceSpec, KeyEvent

VENDOR_USAGE_PAGE = 0xFFA0

# Development only. When True and no real deck is plugged in, Deck.open()
# returns the real Deck class over an in-memory transport instead of raising,
# so the daemon and the configuration page can be worked on without hardware.
# This lives in the code on purpose: there is no config key, flag or
# environment variable for it, so a shipped build can never do it by accident.
# tools/build.py refuses to build while it is True.
#
# Off now that the deck is here: with no fake, an unplugged deck makes the run
# loop wait and reconnect to the real hardware when it comes back, instead of
# silently switching to a fake that hides the disconnect.
FAKE_DECK_WHEN_ABSENT = False

FAKE_SPEC = DeviceSpec(
    name="Fake deck (no hardware, development only)",
    vendor_id=0x0000,
    product_id=0x0000,
    packet_size=1024,
    image_size=95,
    protocol_version=3,
)


class Transport(_Protocol):
    """<summary>
    The four calls the Deck needs from whatever carries its bytes.
    </summary>
    <remarks>
    Kept deliberately small so the tests can supply a recording fake with no
    USB behind it. Implementations must be safe to call from the reader
    thread and the writer thread at once, or be used from one thread only,
    which is what the controller does.
    </remarks>
    """

    def write(self, data: bytes) -> int:
        """<summary>Send one report and return the number of bytes accepted.</summary>
        <remarks>A count short of the full packet is a fault, not a partial
        write to retry: the caller raises rather than sending the rest.</remarks>"""
        ...

    def read(self, length: int, timeout_ms: int) -> bytes:
        """<summary>Wait up to ``timeout_ms`` for one input report.</summary>
        <returns>The bytes read, or empty on timeout.</returns>
        <remarks>A timeout is normal and must not raise: the reader polls
        constantly and an exception per idle poll would swamp the log.</remarks>"""
        ...

    def reopen(self) -> None:
        """<summary>Close and reopen the underlying device after a read error.</summary>
        <exception cref="DeviceNotFound">The device is no longer present.</exception>"""
        ...

    def close(self) -> None:
        """<summary>Release the device. Must tolerate being called twice.</summary>"""
        ...


class DeviceNotFound(RuntimeError):
    """<summary>
    No known deck is plugged in, or the one being used has gone away.
    </summary>
    <remarks>
    Raised on a deliberate unplug as well as a genuine fault, so the run loop
    catches it and waits for the deck to come back rather than exiting. On
    Linux this is also what an uninstalled udev rule looks like, because the
    device enumerates but cannot be opened: check
    ``deploy/install-udev.sh`` before assuming the hardware is at fault.
    </remarks>"""


def enumerate_known() -> list[tuple[DeviceSpec, bytes]]:
    """<summary>
    Every known deck currently plugged in, with the path of its vendor
    interface.
    </summary>
    <returns>A list of (spec, path) pairs, empty when nothing is plugged in.</returns>
    <remarks>
    hidapi is imported here rather than at module scope so the pure logic in
    this file can be imported and tested on a machine with no HID library at
    all. An empty list is the normal answer, not an error.
    </remarks>
    
    <exception cref="ImportError">hidapi is not installed.</exception>"""
    import hid

    found: list[tuple[DeviceSpec, bytes]] = []
    for spec in protocol.KNOWN_DEVICES:
        infos = hid.enumerate(spec.vendor_id, spec.product_id)
        path = _pick_vendor_interface(infos)
        if path is not None:
            found.append((spec, path))
    return found


def _pick_vendor_interface(infos: list[dict]) -> bytes | None:
    """<summary>
    Pick the vendor defined interface out of everything a deck exposes.
    </summary>
    <param name="infos">hidapi enumeration entries for one USB id.</param>
    <returns>The path of the interface to open, or None if there is none.</returns>
    <remarks>
    The deck also enumerates as a keyboard, and opening that one gets a
    handle that accepts writes and does nothing visible. Preference is the
    vendor usage page, then interface 0, which is what the community drivers
    settled on across both firmware generations.
    </remarks>
    """
    for info in infos:
        if info.get("usage_page") == VENDOR_USAGE_PAGE:
            return info["path"]
    for info in infos:
        if info.get("interface_number") == 0:
            return info["path"]
    return None


class HidTransport:
    """<summary>
    A transport backed by hidapi, which is the real hardware path on both
    Windows and Linux.
    </summary>
    <remarks>
    Opened blocking on purpose: reads take an explicit timeout, and
    non-blocking mode turns every idle poll into a busy loop. The handle is
    replaced wholesale by <see cref="reopen"/> rather than reset, because a
    handle that has errored once on Windows does not recover.
    </remarks>
    """

    def __init__(self, spec: DeviceSpec, path: bytes) -> None:
        """<summary>Open one deck at a known interface path.</summary>
        <param name="spec">The spec for this device, which fixes packet and
        image sizes.</param>
        <param name="path">Interface path from
        <see cref="_pick_vendor_interface"/>, not the USB id.</param>
        <exception cref="OSError">The device could not be opened. On Linux
        this is usually the udev rule, not the hardware.</exception>
        """
        import hid

        self._hid = hid
        self.spec = spec
        self.path = path
        self._handle = hid.device()
        self._handle.open_path(path)
        self._handle.set_nonblocking(False)

    def write(self, data: bytes) -> int:
        """<summary>Send one report to the deck.</summary>
        <returns>Bytes accepted by hidapi.</returns>"""
        return self._handle.write(data)

    def read(self, length: int, timeout_ms: int) -> bytes:
        """<summary>Wait up to ``timeout_ms`` for one input report.</summary>
        <returns>The bytes read, or empty on timeout.</returns>"""
        return bytes(self._handle.read(length, timeout_ms))

    def reopen(self) -> None:
        """<summary>Close the handle and open the device again.</summary>
        <remarks>
        The interface path is looked up again rather than reused: a replug
        gives the same deck a different path, so the stored one is stale
        exactly when this is needed most.
        </remarks>
        
        <exception cref="DeviceNotFound">The deck is no longer present.</exception>"""
        self.close()
        path = _pick_vendor_interface(self._hid.enumerate(self.spec.vendor_id, self.spec.product_id))
        if path is None:
            raise DeviceNotFound(f"{self.spec.name} ({self.spec.usb_id}) is no longer present")
        self.path = path
        self._handle = self._hid.device()
        self._handle.open_path(path)
        self._handle.set_nonblocking(False)

    def close(self) -> None:
        """<summary>Release the device, ignoring an already closed handle.</summary>
        <remarks>Swallows OSError so shutdown and the error path can both
        call it without ordering care.</remarks>"""
        try:
            self._handle.close()
        except OSError:
            pass

    def strings(self) -> dict[str, str | None]:
        """<summary>The deck's manufacturer, product and serial strings.</summary>
        <returns>A dict of the three, each None when unreadable.</returns>
        <remarks>
        Identification only, for the probe and devices commands. Every value
        is None rather than raising when the device will not answer, because
        a deck that refuses these still drives perfectly well.
        </remarks>
        """
        try:
            return {
                "manufacturer": self._handle.get_manufacturer_string(),
                "product": self._handle.get_product_string(),
                "serial": self._handle.get_serial_number_string(),
            }
        except OSError:
            return {"manufacturer": None, "product": None, "serial": None}


class MemoryTransport:
    """<summary>
    A transport with nothing behind it, for the fake deck.
    </summary>
    <remarks>
    Writes are counted and the last command name kept, so the fake can be
    inspected; reads wait out their timeout and report no key events.
    Everything sent still passes through <see cref="Deck._send"/> and its
    gate, so the fake exercises the safety check rather than bypassing it.
    </remarks>
    """

    def __init__(self) -> None:
        """<summary>Start with nothing written and no command seen.</summary>"""
        self.packets = 0
        self.last_command: bytes | None = None

    def write(self, data: bytes) -> int:
        """<summary>Count the packet and remember any command name in it.</summary>
        <returns>The full length, so the caller's short write check passes.</returns>"""
        self.packets += 1
        name = protocol.command_name(data)
        if name is not None:
            self.last_command = name
        return len(data)

    def read(self, length: int, timeout_ms: int) -> bytes:
        """<summary>Sleep out the timeout and report no key event.</summary>
        <remarks>The sleep is what stops a fake deck spinning the reader
        thread at full speed.</remarks>"""
        time.sleep(max(0, timeout_ms) / 1000)
        return b""

    def reopen(self) -> None:
        """<summary>Nothing to reopen. Present to satisfy the interface.</summary>"""
        pass

    def close(self) -> None:
        """<summary>Nothing to release. Present to satisfy the interface.</summary>"""
        pass


class Deck:
    """<summary>
    One connected deck. Positions are (row, column) from the top left.
    </summary>
    <remarks>
    Drawing is queued and nothing reaches the screens until
    <see cref="commit"/> is called, so a page is built up and shown in one
    go. Key numbers on the wire count from 1 and are not a grid position;
    the ``set_position_*`` calls convert, and the plain ``set_key_*`` calls
    do not.
    </remarks>
    """

    def __init__(self, transport: Transport, spec: DeviceSpec, allow_experimental: bool = False) -> None:
        """<summary>Wrap an open transport as a deck.</summary>
        <param name="transport">Something already opened and ready to write.</param>
        <param name="spec">The spec for this device, which fixes packet and
        image sizes.</param>
        <param name="allow_experimental">Lift the send gate. The daemon never
        sets this, and neither does anything shipped. It exists for a
        reviewed capture replay on a deck that can be spared, and two tests
        pin it shut.</param>
        """
        self.transport = transport
        self.spec = spec
        self.packet_size = spec.packet_size
        self.image_size = spec.image_size
        # The daemon never sets this. Only a deliberate test script may, and
        # only on a deck that can be spared: see the safety rule in README.md.
        self.allow_experimental = allow_experimental

    @classmethod
    def open(cls, vendor_id: int | None = None, product_id: int | None = None) -> "Deck":
        """<summary>
        Open the first known deck, or the one with the given USB id.
        </summary>
        <param name="vendor_id">USB vendor id, or None for the first deck found.</param>
        <param name="product_id">USB product id, or None for the first deck found.</param>
        <returns>An open deck, with the send gate shut.</returns>
        <remarks>
        With <see cref="FAKE_DECK_WHEN_ABSENT"/> on and nothing plugged in, a
        fake deck in memory is returned instead. Asking for a specific USB id
        never gets the fake, so a caller that means the real hardware can say
        so and get an error rather than a silent stand in.
        </remarks>
        
        <exception cref="DeviceNotFound">Nothing matching is plugged in.</exception>"""
        try:
            candidates = enumerate_known()
        except ImportError:  # hidapi missing: only tolerable on a development machine
            if not FAKE_DECK_WHEN_ABSENT:
                raise
            candidates = []
        if vendor_id is not None and product_id is not None:
            candidates = [(s, p) for s, p in candidates
                          if s.vendor_id == vendor_id and s.product_id == product_id]
        if not candidates:
            if FAKE_DECK_WHEN_ABSENT and vendor_id is None and product_id is None:
                return cls(MemoryTransport(), FAKE_SPEC)
            raise DeviceNotFound("no known deck is plugged in")
        spec, path = candidates[0]
        return cls(HidTransport(spec, path), spec)

    @property
    def is_fake(self) -> bool:
        """<summary>
        True for the development deck in memory, which has no hardware
        behind it.
        </summary>
        <remarks>
        Worth surfacing in the interface: everything appears to work against
        a fake, so a page that looks right while nothing lights up is
        explained by this rather than by a drawing bug.
        </remarks>
        """
        return isinstance(self.transport, MemoryTransport)

    def close(self) -> None:
        """<summary>Release the device.</summary>
        <remarks>Does not blank the screens: the deck keeps showing the last
        committed page after the daemon exits, which is deliberate.</remarks>"""
        self.transport.close()

    def __enter__(self) -> "Deck":
        """<summary>Support ``with Deck.open() as deck``.</summary>"""
        return self

    def __exit__(self, *exc) -> None:
        """<summary>Close the deck on leaving the block, error or not.</summary>"""
        self.close()

    # Outgoing

    def _send(self, packet: bytes) -> None:
        """<summary>
        Write one report, refusing any command outside the proven set.
        </summary>
        <param name="packet">A full report, report id byte included.</param>
        <remarks>
        This is the last gate before the USB link, so it catches packets
        built by hand as well as those from <see cref="protocol.command"/>.
        Image chunks carry no command name and pass through, which is correct:
        the header that describes them was already checked.

        A deck was permanently disabled on 10 September 2026 by commands sent
        without this check. Do not widen the set here. A new command is added
        only to <see cref="protocol.SAFE_COMMANDS"/>, only as a byte for byte
        copy of a capture of the official app, and only after review.
        </remarks>
        
        <exception cref="protocol.UnsafeCommand">The packet carries a command
        that is not proven and the deck was not opened with
        ``allow_experimental``.</exception>
        <exception cref="OSError">The transport accepted fewer bytes than the
        packet holds.</exception>"""
        if not self.allow_experimental:
            name = protocol.command_name(packet)
            if name is not None and name not in protocol.SAFE_COMMANDS:
                raise protocol.UnsafeCommand(
                    f"refusing to send {name!r}: not a proven command (see the safety rule in README.md)")
        written = self.transport.write(packet)
        if written != len(packet):
            raise OSError(f"short write: {written} of {len(packet)} bytes")

    def initialise(self) -> None:
        """<summary>Send the display init and handshake pair, once, after opening.</summary>
        <remarks>Must run before anything is drawn, and again after a replug.
        Skipping it leaves the screens dark however much is committed.</remarks>"""
        for packet in protocol.init_commands(self.packet_size):
            self._send(packet)

    def set_brightness(self, percent: int) -> None:
        """<summary>Set the backlight for the whole deck.</summary>
        <param name="percent">0 to 100, clamped rather than rejected.</param>
        <remarks>Takes effect at once, with no commit needed. 0 is off but
        still awake; use <see cref="sleep"/> to sleep it.</remarks>"""
        self._send(protocol.brightness_command(percent, self.packet_size))

    def clear(self, key: int | None = None) -> None:
        """<summary>Blank one device key number, or every key.</summary>
        <param name="key">Device key number from 1, or None for all of them.</param>
        <remarks>Queued like any drawing: nothing changes until
        <see cref="commit"/>. Clearing all keys is also what returns the
        surround to dark after a replug.</remarks>"""
        self._send(protocol.clear_command(key, self.packet_size))

    def sleep(self) -> None:
        """<summary>Put the screens to sleep. Keys still report presses.</summary>
        <remarks>There is no wake command: the deck wakes on the next image
        and commit, so waking means redrawing the current page.</remarks>"""
        self._send(protocol.sleep_command(self.packet_size))

    def keepalive(self) -> None:
        """<summary>Send the periodic handshake that keeps the deck listening.</summary>
        <remarks>The official app sends this every ten seconds. Let it lapse
        and the deck quietly stops reporting presses while still looking
        connected and still showing its page.</remarks>"""
        self._send(protocol.keepalive_command(self.packet_size))

    def set_key_jpeg(self, key: int, jpeg: bytes) -> None:
        """<summary>Queue a ready made JPEG on a device key number.</summary>
        <param name="key">Device key number from 1, not a grid position.</param>
        <param name="jpeg">Encoded bytes already at the deck's image size and
        orientation.</param>
        <remarks>Nothing is checked about the image here beyond its length.
        Needs <see cref="commit"/> afterwards.</remarks>"""
        for packet in protocol.image_packets(key, jpeg, self.packet_size):
            self._send(packet)

    def set_key_image(self, key: int, image) -> None:
        """<summary>Encode an image for this deck and queue it on a key number.</summary>
        <param name="key">Device key number from 1, not a grid position.</param>
        <param name="image">A Pillow image. Resized and rotated to suit the
        deck by the images module.</param>
        <remarks>Encodes on every call, so for an animation encode once and
        use <see cref="set_key_jpeg"/> per frame.</remarks>"""
        self.set_key_jpeg(key, images.encode(image, self.image_size))

    def set_position_image(self, row: int, column: int, image) -> None:
        """<summary>Queue an image at a grid position rather than a key number.</summary>
        <param name="row">Row from 0 at the top.</param>
        <param name="column">Column from 0 at the left.</param>
        <param name="image">A Pillow image.</param>
        <remarks>The position to key number conversion is the layout
        module's, and is the only correct place for it.</remarks>"""
        self.set_key_image(layout.key_number(row, column), image)

    def set_position_jpeg(self, row: int, column: int, jpeg: bytes) -> None:
        """<summary>Queue a ready made JPEG at a grid position.</summary>
        <param name="row">Row from 0 at the top.</param>
        <param name="column">Column from 0 at the left.</param>
        <param name="jpeg">Bytes already in device orientation and size.</param>
        <remarks>The animation path: frames are encoded once and replayed,
        which is why this skips the encode step.</remarks>"""
        self.set_key_jpeg(layout.key_number(row, column), jpeg)

    def commit(self) -> None:
        """<summary>Push everything queued since the last commit onto the screens.</summary>
        <remarks>Nothing drawn is visible until this runs. A page that stays
        blank is far more often a missing commit than an encoding fault, so
        check for it first.</remarks>"""
        self._send(protocol.commit_command(self.packet_size))

    # Incoming

    def read_event(self, timeout_ms: int = 250) -> KeyEvent | None:
        """<summary>Wait for the next key event.</summary>
        <param name="timeout_ms">How long to wait before giving up.</param>
        <returns>The event, or None on timeout, on a report that is not a key
        event, or after recovering from a read error.</returns>
        <remarks>
        None is the common answer and never means trouble. A read error is
        treated as a lost handle rather than a fatal fault: hidapi raised one
        transient error per session on Windows with the deck still enumerated,
        so this pauses, reopens and returns None to let the caller poll again.
        </remarks>
        """
        try:
            data = self.transport.read(protocol.REPORT_LENGTH, timeout_ms)
        except OSError:
            time.sleep(0.5)
            self.transport.reopen()
            return None
        if not data:
            return None
        return protocol.parse_report(data)
