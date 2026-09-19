"""<summary>
Wire format for the Mirabox 293S family, as spoken by the Soomfon XF-CN001.
Pure functions only. Nothing here touches USB, so all of it is unit tested
against the packets captured from the real deck.
</summary>
<remarks>
Every outgoing packet is one HID output report: a zero report id byte followed
by exactly ``packet_size`` bytes. Commands begin with the ASCII text CRT and
two zero bytes, then a short command name and its arguments. Images are sent
as a BAT header followed by the raw JPEG bytes in packet sized chunks, and a
final STP commits everything to the screens.

Incoming packets are 512 byte input reports that begin with the text ACK. Byte
9 holds the key number counting from 1 and byte 10 holds 1 for a press or 0
for a release. The deck sends nothing else.

Building a packet here is not the same as being allowed to send one. This
module refuses to build anything outside <see cref="SAFE_COMMANDS"/> without
an explicit opt in, and <see cref="device.Deck"/> refuses to write it. Both
gates have to be passed deliberately, and the daemon passes neither.
</remarks>
"""

from __future__ import annotations

from dataclasses import dataclass

PREFIX = b"\x00CRT\x00\x00"
REPORT_LENGTH = 512
KEY_COUNT = 18
MAX_IMAGE_BYTES = 0xFFFF


@dataclass(frozen=True)
class DeviceSpec:
    """<summary>
    What differs between firmware generations of the same hardware.
    </summary>
    <remarks>
    Two decks can carry the same model name and still disagree on packet
    size, image size and protocol version, so nothing here may be assumed
    from the name. Values come from a capture of the real device, never
    from a guess. The frozen dataclass is deliberate: a spec is shared
    across threads and must not be edited after it is chosen.
    </remarks>"""

    name: str
    vendor_id: int
    product_id: int
    packet_size: int
    image_size: int
    protocol_version: int
    # The keys are regions of one screen of this size. Setting a whole screen
    # picture is NOT supported: the command the deck has for it stored a bad
    # picture and, with a mode command, disabled a deck. See REVIEW.md.
    screen: tuple[int, int] = (854, 480)

    @property
    def usb_id(self) -> str:
        """<summary>
        The vendor and product ids as lowercase hex joined by a colon.
        </summary>
        <returns>A string such as "1500:3003".</returns>
        <remarks>
        This is the spelling lsusb and the udev rules file use, so it is
        what to print when telling someone which rule to install.
        </remarks>
        """
        return f"{self.vendor_id:04x}:{self.product_id:04x}"


SOOMFON_XF_CN001 = DeviceSpec(
    name="Soomfon Stream Controller XF-CN001",
    vendor_id=0x1500,
    product_id=0x3003,
    packet_size=1024,
    image_size=95,
    protocol_version=3,
    screen=(854, 480),
)

SOOMFON_CN001 = DeviceSpec(
    name="Soomfon Studio Control Deck CN001 (Mirabox HSV293S)",
    vendor_id=0x5548,
    product_id=0x6670,
    packet_size=512,
    image_size=85,
    protocol_version=1,
    screen=(800, 480),
)

KNOWN_DEVICES: tuple[DeviceSpec, ...] = (SOOMFON_XF_CN001, SOOMFON_CN001)


def spec_for(vendor_id: int, product_id: int) -> DeviceSpec | None:
    """<summary>
    Find the spec for a USB id, or None when the device is not one we know.
    </summary>
    <param name="vendor_id">USB vendor id, as reported by the HID layer.</param>
    <param name="product_id">USB product id, as reported by the HID layer.</param>
    <returns>The matching spec, or None for an unknown device.</returns>
    <remarks>
    None means do not open it. An unknown deck may well be a sibling that
    speaks nearly the same protocol, but "nearly" is how a deck was lost:
    the caller must add a spec from a capture rather than fall back to a
    similar one. See <see cref="KNOWN_DEVICES"/>.
    </remarks>
    """
    for spec in KNOWN_DEVICES:
        if spec.vendor_id == vendor_id and spec.product_id == product_id:
            return spec
    return None


def pad(payload: bytes, packet_size: int) -> bytes:
    """<summary>
    Pad a payload out to the report id byte plus one full packet.
    </summary>
    <param name="payload">The bytes so far, report id byte included.</param>
    <param name="packet_size">The deck's packet size, from its spec.</param>
    <returns>Exactly ``1 + packet_size`` bytes.</returns>
    <remarks>
    Every report must be this exact length. Windows rejects a short write
    outright, and Linux accepts it quietly and leaves the deck waiting for
    the rest, which looks like a hang rather than an error. Padding here
    rather than at each call site is what stops that being rediscovered.
    </remarks>
    
    <exception cref="ValueError">The payload is longer than one packet.</exception>"""
    full = 1 + packet_size
    if len(payload) > full:
        raise ValueError(f"payload of {len(payload)} bytes exceeds packet of {full}")
    return payload + b"\x00" * (full - len(payload))


# The only commands the daemon may send. Every one of these has been used on
# this deck for days and by the community drivers for years. The deck also
# understands LOG, BGPIC, BGCLE, MOD and others, and on 10 September 2026 a
# LOG with the wrong picture followed by a MOD left a deck unable to start
# USB at all. Nothing outside this set goes out unless the caller says, in
# so many words, that it is experimenting.
SAFE_COMMANDS = frozenset({b"DIS", b"CONNECT", b"LIG", b"CLE", b"HAN", b"BAT", b"STP"})


class UnsafeCommand(ValueError):
    """<summary>
    Raised when a command outside <see cref="SAFE_COMMANDS"/> is built
    without the caller explicitly saying it is experimenting.
    </summary>
    <remarks>
    This is a safety gate, not an argument check, so never catch it to
    carry on. Reaching it means the code tried to invent a command, which
    is the exact sequence that permanently disabled a deck on 10 September
    2026. The fix is a USB capture of the official app, not a wider set.
    </remarks>"""


def command(name: bytes, *args: int, packet_size: int, experimental: bool = False) -> bytes:
    """<summary>
    Build one command packet: the prefix, the command name, then its
    arguments, padded to a full report.
    </summary>
    <param name="name">Command name in ASCII capitals, such as b"LIG".</param>
    <param name="args">Argument bytes, in the order the deck expects them.</param>
    <param name="packet_size">The deck's packet size, from its spec.</param>
    <param name="experimental">Opt in to building a command outside the
    proven set. The daemon never passes this. It exists so a capture can be
    replayed byte for byte under review, and for the tests that pin this
    gate shut.</param>
    <returns>One padded packet, ready to write.</returns>
    <remarks>
    This is the first of two gates. Passing it only means the bytes were
    built: <see cref="device.Deck"/> still refuses to write an experimental
    packet unless it was opened with ``allow_experimental``. Two tests hold
    both gates shut and must never be weakened.
    </remarks>
    
    <exception cref="UnsafeCommand">``name`` is not proven and
    ``experimental`` was not set.</exception>"""
    if name not in SAFE_COMMANDS and not experimental:
        raise UnsafeCommand(f"{name!r} is not a proven command; pass experimental=True only in a test script")
    return pad(PREFIX + name + bytes(args), packet_size)


def command_name(packet: bytes) -> bytes | None:
    """<summary>
    Read back the command a packet carries, or None for image data and
    other reports.
    </summary>
    <param name="packet">A packet as built by <see cref="command"/>.</param>
    <returns>The command name, or None when the packet is not a command.</returns>
    <remarks>
    Commands are the prefix followed by the name, then either the end of
    the meaningful bytes or a zero. The longest name is CONNECT at seven
    letters, which is why only eight bytes are examined. This exists so the
    tests and the capture decoder can assert what was actually sent rather
    than what was meant to be sent.
    </remarks>
    """
    if not packet.startswith(PREFIX):
        return None
    rest = packet[len(PREFIX):len(PREFIX) + 8]
    name = bytearray()
    for byte in rest:
        if 0x41 <= byte <= 0x5A:
            name.append(byte)
        else:
            break
    return bytes(name) if name else None


def init_commands(packet_size: int) -> list[bytes]:
    """<summary>
    The two packets sent once after opening: display init, then the keep
    alive handshake.
    </summary>
    <param name="packet_size">The deck's packet size, from its spec.</param>
    <returns>DIS then CONNECT, in that order.</returns>
    <remarks>
    The order matters and is not interchangeable: a CONNECT before DIS
    leaves the screens dark until the next commit. A capture of the
    official app shows the same pair in the same order.
    </remarks>
    """
    return [command(b"DIS", packet_size=packet_size),
            command(b"CONNECT", packet_size=packet_size)]


def brightness_command(percent: int, packet_size: int) -> bytes:
    """<summary>
    Set the backlight brightness for the whole deck.
    </summary>
    <param name="percent">Brightness from 0 to 100. Values outside that
    range are clamped rather than rejected, because this is driven by a
    config file and a slider, and a typo should dim a deck rather than
    stop the daemon.</param>
    <param name="packet_size">The deck's packet size, from its spec.</param>
    <returns>One LIG packet.</returns>
    <remarks>
    0 is genuinely off, not merely dim, and the deck stays awake at 0. Use
    <see cref="sleep_command"/> to actually sleep it.
    </remarks>
    """
    level = max(0, min(100, int(percent)))
    return command(b"LIG", 0x00, 0x00, level, packet_size=packet_size)


def clear_command(key: int | None, packet_size: int) -> bytes:
    """<summary>
    Blank one key, or every key at once.
    </summary>
    <param name="key">Device key number counting from 1, or None for all
    of them.</param>
    <param name="packet_size">The deck's packet size, from its spec.</param>
    <returns>One CLE packet.</returns>
    <remarks>
    Clearing all keys is also what puts the surround back to dark after a
    USB drop, which is why the controller sends it on connect. That
    behaviour was confirmed from a capture of the official app, not
    guessed.
    </remarks>
    
    <exception cref="ValueError">``key`` is outside 1 to 18.</exception>"""
    target = 0xFF if key is None else _check_key(key)
    return command(b"CLE", 0x00, 0x00, 0x00, target, packet_size=packet_size)


def sleep_command(packet_size: int) -> bytes:
    """<summary>
    Put the deck's screens to sleep. Keys still report presses.
    </summary>
    <param name="packet_size">The deck's packet size, from its spec.</param>
    <returns>One HAN packet.</returns>
    <remarks>
    There is no matching wake command. The deck wakes on the next image or
    commit, so waking means redrawing the current page.
    </remarks>
    """
    return command(b"HAN", packet_size=packet_size)


def keepalive_command(packet_size: int) -> bytes:
    """<summary>
    The periodic handshake that stops the deck deciding the host has gone.
    </summary>
    <param name="packet_size">The deck's packet size, from its spec.</param>
    <returns>One CONNECT packet, the same bytes as the one sent at init.</returns>
    <remarks>
    A capture of the official app shows it sending this every ten seconds.
    Stop sending it and the deck eventually stops reporting key presses
    while still looking perfectly alive.
    </remarks>
    """
    return command(b"CONNECT", packet_size=packet_size)


def commit_command(packet_size: int) -> bytes:
    """<summary>
    Commit everything written since the last commit to the screens.
    </summary>
    <param name="packet_size">The deck's packet size, from its spec.</param>
    <returns>One STP packet.</returns>
    <remarks>
    Nothing drawn appears until this is sent. Forgetting it looks exactly
    like an image encoding bug, so check for it first. Batching a whole
    page and committing once is both faster and free of tearing.
    </remarks>
    """
    return command(b"STP", packet_size=packet_size)


def image_packets(key: int, jpeg: bytes, packet_size: int) -> list[bytes]:
    """<summary>
    Split one key's JPEG into the BAT header packet followed by the image
    bytes in packet sized chunks.
    </summary>
    <param name="key">Device key number counting from 1.</param>
    <param name="jpeg">Encoded JPEG bytes for that key, already at the
    deck's image size.</param>
    <param name="packet_size">The deck's packet size, from its spec.</param>
    <returns>The header packet followed by one packet per chunk.</returns>
    <remarks>
    The length lives in two bytes of the header, so an image over 65535
    bytes cannot be described and is refused here rather than truncated.
    The final chunk is padded like any other, and the deck ignores the
    padding because it stops reading at the declared length. Nothing
    appears until <see cref="commit_command"/> follows.
    </remarks>
    
    <exception cref="ValueError">The key is out of range, the image is
    empty, or it is too big for the 16 bit length field.</exception>"""
    _check_key(key)
    size = len(jpeg)
    if size == 0:
        raise ValueError("empty image")
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"image of {size} bytes exceeds the 16 bit length field")
    header = command(b"BAT", 0x00, 0x00, (size >> 8) & 0xFF, size & 0xFF, key,
                     packet_size=packet_size)
    packets = [header]
    for offset in range(0, size, packet_size):
        packets.append(pad(b"\x00" + jpeg[offset:offset + packet_size], packet_size))
    return packets


@dataclass(frozen=True)
class KeyEvent:
    """<summary>
    One press or release, as reported by the deck.
    </summary>
    <remarks>
    ``key`` is the device number counting from 1 to 18, which is the deck's
    own numbering and not a row and column. Converting to a grid position
    is the layout module's job, so that this stays a faithful record of
    what arrived on the wire.
    </remarks>"""

    key: int
    pressed: bool


def parse_report(data: bytes) -> KeyEvent | None:
    """<summary>
    Decode an input report into a key event.
    </summary>
    <param name="data">Raw bytes of one input report.</param>
    <returns>The event, or None when the report is not a key event.</returns>
    <remarks>
    None is normal and frequent rather than an error: the deck sends short
    and unrelated reports, and a reader that treats None as a fault will
    log constantly. Anything malformed, truncated or carrying a key number
    outside 1 to 18 is discarded rather than guessed at.
    </remarks>
    """
    if len(data) < 11 or data[:3] != b"ACK":
        return None
    key = data[9]
    if not 1 <= key <= KEY_COUNT:
        return None
    return KeyEvent(key=key, pressed=data[10] == 1)


def _check_key(key: int) -> int:
    """
    <summary>
    Reject a key number outside 1 to KEY_COUNT.
    </summary>
    <param name="key">The key number.</param>
    <returns>The same number.</returns>
    <exception cref="ValueError">When it is out of range.</exception>
    """
    if not 1 <= key <= KEY_COUNT:
        raise ValueError(f"key {key} is outside 1 to {KEY_COUNT}")
    return key
