"""<summary>
Decode a USBPcap capture of the deck and list what the official app sent.

    python tools/decode_capture.py tools/captures/hub1.pcap tools/captures/hub2.pcap

Run it on the hub*.pcap files that capture-deck.bat produced. With no paths it
decodes every ``*.pcap`` beside this file in ``tools/captures``.
</summary>
<remarks>
This tool reads pcap files and nothing else. It never opens a USB device, never
writes to one and sends no command to any deck, so it is safe to run with the
deck plugged in, unplugged or on a machine that has never seen one. It is the
safe half of the rule that the deck is learned by listening and never by
sending: the capture is taken on the Windows machine with the official app
driving the hardware, and everything understood about the wire format is read
back out here, from the file, afterwards. Nothing this tool prints is a licence
to send a command that has not been reviewed.

It scans every USB packet for the two magics the deck family uses:

  - outgoing commands from the app to the deck begin with CRT (0x43 0x52 0x54),
    then two zero bytes, then a short upper case name and its argument bytes.
  - incoming reports from the deck begin with ACK (0x41 0x43 0x4b).

For each file it prints a per command summary (how many, first and last time,
an example of the argument bytes) and a chronological log. When it sees a BAT
image header it can reassemble the JPEG that follows and write it next to the
capture, so the size and form of key and background pictures can be inspected.

A USBPcap capture is of a whole hub, not of one device, so most files contain
other devices' traffic and some contain no deck traffic at all. That is why the
tool is pointed at several files at once and says plainly when a file holds no
CRT commands and no ACK reports.

No third party packages are used, so it runs anywhere Python does.
</remarks>"""

from __future__ import annotations

import struct
import sys
from collections import defaultdict
from pathlib import Path

# USBPcap link layer type in the pcap global header.
DLT_USBPCAP = 249

# Reassembled images are written only when this is on (pass --dump-images).
# A capture shared with a noisy device (a mouse jiggler, a webcam) produces a
# lot of chunks, so it is off by default and the command summary is what prints.
DUMP_IMAGES = False
# Only write reassembled data that actually starts with a JPEG marker.
JPEG_SOI = b"\xff\xd8\xff"


def read_pcap_records(path: Path):
    """<summary>
    Walk a classic pcap file and yield one tuple per captured USB packet.
    </summary>
    <param name="path">The ``.pcap`` file to read. It is read whole into memory,
    which is fine for the short captures this tool is aimed at.</param>
    <returns>
    A generator of ``(timestamp_seconds, usb_payload, endpoint, transfer)``.
    The timestamp is absolute seconds; callers make it relative themselves.
    ``usb_payload`` is the USB data with the USBPcap packet header stripped off.
    </returns>
    <remarks>
    Both byte orders and both timestamp resolutions of the classic format are
    accepted, because which one a capture carries depends on the tool that
    wrote it. A link type other than USBPcap is a warning rather than an error,
    so an odd capture can still be looked at.

    Truncated records at the tail of a file are skipped rather than raising: a
    capture stopped by hand often ends mid record, and that is not a fault
    worth losing the rest of the file over.
    </remarks>
    
    <exception cref="ValueError">The file does not begin with a pcap magic, so
    it is not a classic pcap at all. A pcapng file lands here too: this reader
    deliberately does not understand the newer format.</exception>"""
    data = path.read_bytes()
    if len(data) < 24:
        return
    magic = data[:4]
    if magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        endian = ">"
    elif magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        endian = "<"
    else:
        raise ValueError(f"{path.name}: not a pcap file (magic {magic.hex()})")

    network = struct.unpack(endian + "I", data[20:24])[0]
    if network != DLT_USBPCAP:
        print(f"warning: {path.name} link type is {network}, expected {DLT_USBPCAP} (USBPcap)")

    offset = 24
    while offset + 16 <= len(data):
        ts_sec, ts_usec, incl_len, _orig_len = struct.unpack(
            endian + "IIII", data[offset:offset + 16])
        offset += 16
        record = data[offset:offset + incl_len]
        offset += incl_len
        if len(record) < 27:
            continue
        header_len = struct.unpack("<H", record[0:2])[0]
        endpoint = record[21]
        transfer = record[22]
        payload = record[header_len:]
        yield ts_sec + ts_usec / 1_000_000, payload, endpoint, transfer


def command_in(payload: bytes):
    """<summary>
    Pick a CRT command out of one USB payload, if there is one in it.
    </summary>
    <param name="payload">The USB data of a single captured packet.</param>
    <returns>
    ``(name, args)`` with the command name as bytes and the sixteen bytes that
    follow it, or None when the payload carries no CRT command.
    </returns>
    <remarks>
    The magic is searched for anywhere in the payload rather than only at the
    start, because a captured packet may carry a report id or padding in front
    of it depending on how the capture was taken.

    The name is read as the run of upper case ASCII after the magic and the two
    zero bytes, which is how the wire format delimits it. Sixteen argument bytes
    are taken as a fixed window: that is enough to show what any of the known
    commands carried, and it is not an assertion that the command is that long.
    Anything past it is padding as far as this tool is concerned.
    </remarks>
    """
    idx = payload.find(b"CRT\x00\x00")
    if idx == -1:
        return None
    start = idx + 5
    name = bytearray()
    i = start
    while i < len(payload) and 0x41 <= payload[i] <= 0x5A:
        name.append(payload[i])
        i += 1
    if not name:
        return None
    args = payload[i:i + 16]
    return bytes(name), args


def is_ack(payload: bytes) -> bool:
    """
    <summary>
    Whether a payload is one of the deck's ACK replies.
    </summary>
    <param name="payload">Packet bytes.</param>
    <returns>True or False.</returns>
    """
    return payload[:3] == b"ACK"


def hexbytes(data: bytes) -> str:
    """
    <summary>
    Bytes as space separated two digit hex.
    </summary>
    <param name="data">The bytes.</param>
    <returns>A string.</returns>
    """
    return " ".join(f"{b:02x}" for b in data)


def trimmed_args(args: bytes) -> str:
    """<summary>
    Format argument bytes as hex with the trailing zero padding taken off.
    </summary>
    <param name="args">The fixed window of argument bytes from
    <see cref="command_in"/>.</param>
    <returns>Space separated hex, or the text "(none)" when every byte was
    zero.</returns>
    <remarks>
    Only trailing zeroes go: a zero in the middle of a command's arguments is
    a real value and is kept, so two commands that differ only by an embedded
    zero still read differently in the summary.
    </remarks>
    """
    end = len(args)
    while end > 0 and args[end - 1] == 0:
        end -= 1
    return hexbytes(args[:end]) if end else "(none)"


def decode_file(path: Path) -> None:
    """<summary>
    Decode one capture and print its command summary and chronological log.
    </summary>
    <param name="path">A ``.pcap`` file. Reassembled images, when
    <see cref="DUMP_IMAGES"/> is on, are written beside it.</param>
    <remarks>
    Times in the output are relative to the first packet in the file, not to
    the clock, so two captures can be compared by eye. That also means the zero
    point is whenever the capture was started, which on the captures taken so
    far was after the deck was plugged in: the connect sequence is therefore
    missing from them rather than absent from the protocol.

    Image reassembly is deliberately crude. Everything that is neither a
    command nor an ACK after a BAT header is treated as part of that image, and
    the next command ends it. A capture shared with another busy device will
    mix that device's packets into the blob, which is why only data starting
    with a JPEG marker is written out and why writing is off by default.
    </remarks>
    """
    print("=" * 70)
    print(path.name)
    print("=" * 70)

    commands = []          # (ts, name, args)
    acks = 0
    first_ts = None
    reassembly = None      # bytes being collected after a BAT header
    image_index = 0

    for ts, payload, endpoint, transfer in read_pcap_records(path):
        if first_ts is None:
            first_ts = ts
        rel = ts - first_ts

        cmd = command_in(payload)
        if cmd is not None:
            name, args = cmd
            commands.append((rel, name, args))
            # Flush any image that was being collected before this command.
            if reassembly:
                blob = bytes(reassembly)
                image_index += 1
                if DUMP_IMAGES and blob[:3] == JPEG_SOI:
                    out = path.with_name(f"{path.stem}_img{image_index:02d}.jpg")
                    out.write_bytes(blob)
                    print(f"  wrote {out.name}: {len(blob)} bytes")
            reassembly = bytearray() if name == b"BAT" else None
            continue

        if is_ack(payload):
            acks += 1
            continue

        # Not a command and not an ACK. If we are collecting image data after a
        # BAT header, this is a JPEG chunk. Strip a leading report id zero.
        if reassembly is not None and payload:
            chunk = payload[1:] if payload[0] == 0 else payload
            reassembly.extend(chunk)

    if reassembly:
        blob = bytes(reassembly)
        image_index += 1
        if DUMP_IMAGES and blob[:3] == JPEG_SOI:
            out = path.with_name(f"{path.stem}_img{image_index:02d}.jpg")
            out.write_bytes(blob)
            print(f"  wrote {out.name}: {len(blob)} bytes")

    if not commands and not acks:
        print("  no CRT commands and no ACK reports. This hub is not the deck.")
        print()
        return

    print(f"  {len(commands)} commands, {acks} ACK reports from the deck")
    print()

    summary = defaultdict(lambda: {"count": 0, "first": None, "last": None, "example": b""})
    for rel, name, args in commands:
        s = summary[name]
        s["count"] += 1
        if s["first"] is None:
            s["first"] = rel
        s["last"] = rel
        if not s["example"]:
            s["example"] = args

    print("  command summary")
    print(f"    {'name':<9}{'count':>6}  {'first s':>8}  {'last s':>8}  example args")
    for name in sorted(summary):
        s = summary[name]
        print(f"    {name.decode():<9}{s['count']:>6}  {s['first']:>8.2f}  "
              f"{s['last']:>8.2f}  {trimmed_args(s['example'])}")
    print()

    print("  chronological log")
    for rel, name, args in commands:
        print(f"    {rel:>8.2f}  {name.decode():<8} {trimmed_args(args)}")
    print()


def main(argv: list[str]) -> int:
    """<summary>
    Command line entry point: decode each capture named, or every capture in
    ``tools/captures`` when none is named.
    </summary>
    <param name="argv">Arguments after the script name. The flag
    ``--dump-images`` turns image writing on; everything else is a path.</param>
    <returns>0 once the files have been decoded, 1 when there was nothing to
    decode and the usage line was printed.</returns>
    <remarks>
    A path that does not exist is reported and skipped rather than ending the
    run, because the usual invocation names several hub captures at once and
    only some of them were produced.
    </remarks>
    """
    global DUMP_IMAGES
    if "--dump-images" in argv:
        DUMP_IMAGES = True
        argv = [a for a in argv if a != "--dump-images"]
    paths = [Path(a) for a in argv]
    if not paths:
        here = Path(__file__).resolve().parent / "captures"
        paths = sorted(here.glob("*.pcap"))
    if not paths:
        print("usage: python tools/decode_capture.py <capture.pcap> [more.pcap ...]")
        return 1
    for path in paths:
        if not path.exists():
            print(f"skip: {path} does not exist")
            continue
        decode_file(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
