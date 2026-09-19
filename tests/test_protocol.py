"""<summary>
Wire format tests, pinned to packets captured from the real deck.
</summary>
<remarks>
There is no vendor specification for this hardware, so every byte expected in
this file was read off a USB capture of the official application driving a real
deck. That makes these assertions the record and the code the thing that has to
agree with them: editing an expected value to make a test pass throws away the
only evidence of what the deck actually accepts.

A failure here means the daemon would put bytes on the wire that were never
observed to work. That is not a cosmetic risk. A packet built on a guess, at
the wrong size, has permanently disabled a deck before now, with no recovery.
</remarks>
"""

import pytest

from deckplate import protocol
from deckplate.protocol import SOOMFON_CN001, SOOMFON_XF_CN001

P = SOOMFON_XF_CN001.packet_size


def test_command_prefix_and_padding():
    """<summary>
    Every command opens with the same nine bytes and is padded to a full report.
    </summary>
    <remarks>
    The leading zero is the HID report id, not payload, which is why the packet is
    one byte longer than ``packet_size``. The deck reads a fixed length report and
    ignores anything short, so a command that stopped being padded would simply
    never take effect, with no error anywhere to say why.
    </remarks>
    """
    packet = protocol.command(b"DIS", packet_size=P)
    assert packet[:9] == bytes([0x00, 0x43, 0x52, 0x54, 0x00, 0x00, 0x44, 0x49, 0x53])
    assert len(packet) == 1 + P
    assert packet[9:] == b"\x00" * (P - 8)


def test_older_firmware_uses_512_byte_packets():
    """<summary>
    The packet size comes from the device spec, never from a constant.
    </summary>
    <remarks>
    The two firmware generations carry the same model name and disagree on report
    length. If this went red because the size had been hard coded to 1024, the
    older deck would take no commands at all while the newer one carried on
    working, which is the hardest kind of fault to reproduce.
    </remarks>
    """
    packet = protocol.command(b"DIS", packet_size=SOOMFON_CN001.packet_size)
    assert len(packet) == 513


def test_pad_refuses_oversized_payload():
    """<summary>
    Padding refuses a payload too big for the report rather than truncating it.
    </summary>
    <remarks>
    Silent truncation is the dangerous alternative: a half command reaching the
    deck is an unknown packet, and unknown packets are exactly what must never be
    sent. Raising keeps the fault at the caller where it can be seen.
    </remarks>
    """
    with pytest.raises(ValueError):
        protocol.pad(b"x" * (P + 2), P)


def test_init_sequence_is_dis_then_connect():
    """<summary>
    Opening a deck sends DIS and then CONNECT, in that order.
    </summary>
    <remarks>
    The order is taken from the capture, not from reasoning about what the names
    mean. Reversing it, or dropping either one, leaves a freshly plugged deck that
    enumerates but shows nothing and reports no presses.
    </remarks>
    """
    dis, connect = protocol.init_commands(P)
    assert dis[6:9] == b"DIS"
    assert connect[6:13] == b"CONNECT"


def test_brightness_bytes_and_clamping():
    """<summary>
    LIG carries a percentage in one byte, clamped to 0 to 100 before it is sent.
    </summary>
    <remarks>
    Nothing is known about how the firmware treats a value above 100, so the clamp
    happens here rather than being left to the deck to interpret. A configuration
    file asking for 250 must reach the hardware as 100, never as 250.
    </remarks>
    """
    assert protocol.brightness_command(80, P)[6:12] == b"LIG\x00\x00\x50"
    assert protocol.brightness_command(250, P)[11] == 100
    assert protocol.brightness_command(-5, P)[11] == 0


def test_clear_one_key_and_all_keys():
    """<summary>
    CLE clears one key by number, or every key when the number is 0xff.
    </summary>
    <remarks>
    Key numbers count from 1 and stop at 18, and an out of range number raises
    instead of being wrapped or clamped. 0xff is the sentinel for all keys, so an
    off by one that produced 0xff for a single key would wipe the whole deck
    instead of one tile.
    </remarks>
    """
    assert protocol.clear_command(7, P)[6:13] == b"CLE\x00\x00\x00\x07"
    assert protocol.clear_command(None, P)[6:13] == b"CLE\x00\x00\x00\xff"
    with pytest.raises(ValueError):
        protocol.clear_command(19, P)


def test_sleep_keepalive_commit_names():
    """<summary>
    HAN, CONNECT and STP are the names behind sleep, keepalive and commit.
    </summary>
    <remarks>
    The keepalive deliberately reuses CONNECT because that is what the official
    application was seen to send; there is no separate ping command and inventing
    one is not allowed. STP is the commit: images written without it are buffered
    and never appear, so a deck that draws nothing is the symptom of this breaking.
    </remarks>
    """
    assert protocol.sleep_command(P)[6:9] == b"HAN"
    assert protocol.keepalive_command(P)[6:13] == b"CONNECT"
    assert protocol.commit_command(P)[6:9] == b"STP"


def test_image_packets_header_and_chunking():
    """<summary>
    A tile is a BAT header naming the key and the JPEG length, then the raw bytes
    split across whole packets.
    </summary>
    <remarks>
    Three details here are easy to get wrong and invisible when you do. The length
    at bytes 11 and 12 is big endian. The chunks that follow carry no header of
    their own, just the report id and payload. The final chunk is zero padded to
    the full packet rather than sent short, because the deck reads a fixed length.

    A wrong length or a short final chunk means the deck reads past the picture
    into whatever follows. Writing a badly sized image is one of the two mistakes
    that destroyed a deck, so this test is about hardware, not appearance.
    </remarks>
    """
    jpeg = bytes(range(256)) * 10  # 2560 bytes, so three chunks at 1024
    packets = protocol.image_packets(4, jpeg, P)
    header, *chunks = packets
    assert header[6:9] == b"BAT"
    assert header[9:11] == b"\x00\x00"
    assert header[11:13] == (2560).to_bytes(2, "big")
    assert header[13] == 4
    assert len(chunks) == 3
    assert all(len(chunk) == 1 + P for chunk in chunks)
    assert all(chunk[0] == 0 for chunk in chunks)
    assert b"".join(chunk[1:] for chunk in chunks)[:2560] == jpeg
    assert chunks[-1][1 + 512:] == b"\x00" * 512  # last chunk zero padded


def test_image_packets_reject_bad_input():
    """<summary>
    An invalid key number, an empty picture and an oversized one are all refused
    before anything is built.
    </summary>
    <remarks>
    The size limit matters most: the length field is two bytes, so a larger JPEG
    would wrap to a small number and the deck would be told to expect far fewer
    bytes than are coming. Refusing here keeps that packet from ever existing.
    </remarks>
    """
    with pytest.raises(ValueError):
        protocol.image_packets(0, b"abc", P)
    with pytest.raises(ValueError):
        protocol.image_packets(1, b"", P)
    with pytest.raises(ValueError):
        protocol.image_packets(1, b"x" * 70000, P)


def _report(key: int, state: int) -> bytes:
    """<summary>
    Build one input report with a key number and state at the captured offsets.
    </summary>
    <param name="key">Key number as the deck sends it, counting from 1.</param>
    <param name="state">1 for a press, 0 for a release.</param>
    <returns>A 512 byte report shaped like the real ones.</returns>
    <remarks>
    The offsets are the whole point: ACK at the start, OK at byte 5, the key at
    byte 9 and the state at byte 10, with everything else zero. Written out by hand
    rather than built through the protocol module, so the parser is checked against
    the capture and not against the code that would have to agree with it anyway.
    </remarks>
    """
    data = bytearray(512)
    data[0:3] = b"ACK"
    data[5:7] = b"OK"
    data[9] = key
    data[10] = state
    return bytes(data)


def test_parse_press_and_release_as_captured():
    """<summary>
    An input report is decoded exactly as the real deck was seen to send it.
    </summary>
    <remarks>
    Byte 9 is the key number counting from 1 and byte 10 is 1 for a press and 0 for
    a release. The comment above the assertion is a literal line from the capture.
    If these offsets drifted, every key would run the action belonging to a
    different key, which looks like a configuration fault rather than a parser one.
    </remarks>
    """
    # 41 43 4b 00 00 4f 4b 00 00 0d 01 ... was key 13 pressed on the real deck
    assert protocol.parse_report(_report(13, 1)) == protocol.KeyEvent(13, True)
    assert protocol.parse_report(_report(13, 0)) == protocol.KeyEvent(13, False)


def test_parse_ignores_junk():
    """<summary>
    Anything that is not a well formed key report decodes to None, never an error.
    </summary>
    <remarks>
    The reader thread calls this on every input report, including the empty results
    a read timeout gives back. Raising instead of returning None would kill the
    reader on the first idle poll, and inventing a key number from a zero filled
    report would fire real actions with nothing pressed.
    </remarks>
    """
    assert protocol.parse_report(b"") is None
    assert protocol.parse_report(b"\x00" * 512) is None
    assert protocol.parse_report(_report(0, 1)) is None
    assert protocol.parse_report(_report(19, 1)) is None


def test_command_name_reads_packets():
    """<summary>
    The command name can be read back out of an assembled packet, and image data
    is recognised as not being a command at all.
    </summary>
    <remarks>
    This is what the safety gate and the log both use to say what a packet is, so a
    JPEG chunk being mistaken for a command name would let image data be judged
    against the safe list and reported as something it is not.
    </remarks>
    """
    assert protocol.command_name(protocol.command(b"DIS", packet_size=P)) == b"DIS"
    assert protocol.command_name(protocol.command(b"CONNECT", packet_size=P)) == b"CONNECT"
    assert protocol.command_name(protocol.brightness_command(50, P)) == b"LIG"
    assert protocol.command_name(protocol.pad(protocol.PREFIX + b"BGPIC" + b"\x00\x00\x01", P)) == b"BGPIC"
    assert protocol.command_name(protocol.pad(b"\x00" + b"\xff\xd8" * 100, P)) is None  # image data
    assert protocol.command_name(b"") is None


def test_unproven_commands_are_refused():
    """<summary>
    Commands outside the proven set are refused unless experimental is passed
    explicitly, and the experimental flag is the only way through.
    </summary>
    <remarks>
    LOG, MOD and the rest disabled a deck once. They must not slip out by accident.

    This test pins the first of the two safety gates shut and must never be
    weakened, loosened or marked expected to fail. If a change makes it red, the
    change is wrong. Adding a name to the safe list is only ever done from a byte
    for byte USB capture of the official application, never from reasoning about
    what a name is likely to do.

    The final assertion is deliberate too: the whole screen background helper stays
    deleted, because the command behind it is one of the pair that destroyed a deck.
    </remarks>
    """
    for name in (b"LOG", b"MOD", b"BGPIC", b"BGCLE", b"LBLIG", b"SETLB", b"QUCMD"):
        with pytest.raises(protocol.UnsafeCommand):
            protocol.command(name, packet_size=P)
    assert protocol.command(b"LOG", packet_size=P, experimental=True)[6:9] == b"LOG"
    for name in protocol.SAFE_COMMANDS:
        assert protocol.command(name, packet_size=P)
    assert not hasattr(protocol, "background_packets")


def test_spec_lookup():
    """<summary>
    A USB vendor and product id maps to its device spec, and an unknown one maps to
    nothing.
    </summary>
    <remarks>
    The spec fixes packet size, image size and protocol version, so picking the
    wrong one sends correctly formed packets of the wrong shape to real hardware.
    Returning None for an unknown id is what keeps the daemon from opening a device
    it has never been proven against.
    </remarks>
    """
    assert protocol.spec_for(0x1500, 0x3003) is SOOMFON_XF_CN001
    assert protocol.spec_for(0x5548, 0x6670) is SOOMFON_CN001
    assert protocol.spec_for(0x1500, 0x3001) is None
    assert SOOMFON_XF_CN001.usb_id == "1500:3003"
