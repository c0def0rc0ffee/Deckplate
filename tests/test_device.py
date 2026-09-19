"""<summary>
Deck behaviour against a fake transport that records every packet.
</summary>
<remarks>
Nothing in this file may ever touch real hardware, and nothing in it may be
copied out into a scratch script and pointed at a deck. The whole reason the
Deck class is written against a transport interface is so that its behaviour
can be proved here, with the bytes inspected afterwards, instead of by
sending anything.

Two tests in this file hold the second safety gate shut:
<see cref="test_hand_built_unproven_packets_are_refused_at_the_device"/> and
the gate assertion inside
<see cref="test_open_returns_a_fake_deck_only_when_the_code_flag_is_on"/>.
They are not ordinary coverage and they must never be weakened, narrowed or
marked expected to fail. On 10 September 2026 a deck was permanently
disabled by hand written packets, an unproven picture command followed by an
unproven mode command; it no longer enumerates and there is no known
recovery. These tests are what stops that code path existing again. If one
of them fails, the correct response is to fix the gate, never the test.
</remarks>
"""

import pytest

from deckplate import images, protocol
from deckplate.device import Deck
from deckplate.protocol import SOOMFON_XF_CN001


class FakeTransport:
    """<summary>
    A transport that keeps every written packet and hands back scripted
    input reports.
    </summary>
    <remarks>
    ``fail_reads`` makes the next few reads raise, which is how the Windows
    quirk of one transient read error per session is reproduced without a
    deck. It counts down, so a value of one fails once and then behaves.
    Reads past the end of ``incoming`` return empty rather than raising,
    because empty is what a real timeout looks like and the reader polls
    constantly.

    ``write`` reports every byte as accepted. A test that needs a short
    write replaces the method outright rather than asking for it here.
    </remarks>
    """

    def __init__(self, incoming=None, fail_reads=0):
        """
        <summary>
        A transport that records writes and replays scripted reads, failing the first fail_reads reads.
        </summary>
        <param name="incoming">Packets to hand back, in order.</param>
        <param name="fail_reads">How many reads raise before the first succeeds.</param>
        """
        self.written = []
        self.incoming = list(incoming or [])
        self.fail_reads = fail_reads
        self.reopened = 0
        self.closed = False

    def write(self, data):
        """<summary>Record a packet and claim all of it was accepted.</summary>"""
        self.written.append(bytes(data))
        return len(data)

    def read(self, length, timeout_ms):
        """<summary>
        The next scripted report, empty on exhaustion, or a raise while
        ``fail_reads`` is still counting down.
        </summary>
        """
        if self.fail_reads:
            self.fail_reads -= 1
            raise OSError("read error")
        return self.incoming.pop(0) if self.incoming else b""

    def reopen(self):
        """<summary>Count a reopen. The scripted input deliberately survives it.</summary>"""
        self.reopened += 1

    def close(self):
        """<summary>Record that the transport was released.</summary>"""
        self.closed = True


def _report(key, state):
    """<summary>
    Build one input report as the deck sends it: 512 bytes beginning ACK,
    with the key number and the press state in their fixed places.
    </summary>
    <param name="key">Key number as the firmware counts them, from 1.</param>
    <param name="state">1 for a press, 0 for a release.</param>
    <returns>A 512 byte report ready to hand to the fake transport.</returns>
    <remarks>
    The offsets are from a capture of the real deck, not from a
    specification, so they are facts rather than choices: byte 9 is the key
    and byte 10 the state. The surrounding bytes are left zero because the
    decoder ignores them. Keep this in step with the parser in
    <see cref="protocol"/> or these tests will pass against a decoder that
    no longer matches the hardware.
    </remarks>
    """
    data = bytearray(512)
    data[0:3] = b"ACK"
    data[5:7] = b"OK"
    data[9] = key
    data[10] = state
    return bytes(data)


@pytest.fixture
def deck():
    """
    <summary>
    A Deck over a fresh FakeTransport with the Soomfon spec.
    </summary>
    <returns>A Deck.</returns>
    """
    return Deck(FakeTransport(), SOOMFON_XF_CN001)


def test_initialise_sends_dis_and_connect(deck):
    """<summary>
    Pins the opening handshake to exactly DIS then CONNECT, in that order
    and with nothing else.
    </summary>
    <remarks>
    This is the sequence observed from the official app, and order matters:
    the deck is told to disconnect from whatever held it before it is told
    to connect. Both are in the proven set. The assertion is an equality
    rather than a containment on purpose, so that an extra command slipped
    into startup fails here rather than being sent to real hardware. Only
    three bytes of the name are compared because CONNECT is longer than the
    slot being sliced.
    </remarks>
    """
    deck.initialise()
    names = [p[6:9] for p in deck.transport.written]
    assert names == [b"DIS", b"CON"]


def test_every_packet_is_exactly_one_report(deck):
    """<summary>
    Pins every outgoing packet, of every kind, to the report id byte plus
    one full packet.
    </summary>
    <remarks>
    The four kinds are covered together because the padding is easy to get
    right for commands and wrong for image chunks, particularly the last
    chunk of a JPEG, which is almost never a full packet's worth. A short
    write is rejected outright by Windows and accepted silently by Linux,
    where it leaves the deck waiting for bytes that never come. That reads
    as a hang rather than an error, which is why this is pinned across the
    whole sequence rather than at the padding helper alone.
    </remarks>
    """
    deck.initialise()
    deck.set_brightness(50)
    deck.set_key_image(1, images.numbered_tile(1, 95))
    deck.commit()
    assert all(len(p) == 1 + 1024 for p in deck.transport.written)


def test_deck_has_no_whole_screen_picture_method(deck):
    """<summary>
    Pins the absence of a whole screen picture method on the Deck.
    </summary>
    <remarks>
    This asserts that something does not exist, which is unusual and
    deliberate. The deck does understand commands that paint the entire
    screen, and one of them, sent with a wrongly sized picture, is half of
    what permanently disabled a deck. There is to be no convenient method
    for it, so that adding one is a visible decision rather than an
    autocomplete. If this goes red, someone has added the method back;
    remove it rather than deleting the test.
    </remarks>
    """
    assert not hasattr(deck, "set_background_colour")


def test_hand_built_unproven_packets_are_refused_at_the_device(deck):
    """<summary>
    The scripts that disabled a deck built packets by hand; this is the gate
    that stops that.
    </summary>
    <remarks>
    This test must never be weakened. It is one of the two that hold the
    hardware safety gates shut, and it covers the lower of the two: a packet
    assembled directly, bypassing <see cref="protocol.command"/> entirely,
    is still refused by <see cref="device.Deck._send"/>. That matters
    because hand assembly is exactly how the packets that killed a deck on
    10 September 2026 were made. The names listed include both of the
    commands involved.

    All three parts are load bearing. Every unproven name must raise, and
    nothing at all may have reached the transport, so a gate that refused
    after writing would still fail here. The proven commands and the image
    data must still go through, or the gate would be useless by being total.
    And the only exception is a deck opened with ``allow_experimental``,
    which exists solely to replay a reviewed capture byte for byte; the
    daemon never passes it, and it must never be passed against a real deck.

    Do not add a name to the safe set to make something here pass. A new
    command is added only as a byte for byte copy of a capture of the
    official app, after review.
    </remarks>
    """
    for name in (b"LOG", b"MOD", b"BGPIC", b"BGCLE", b"LBLIG", b"SETLB", b"QUCMD", b"DELED"):
        packet = protocol.pad(protocol.PREFIX + name + b"\x00\x31", 1024)
        with pytest.raises(protocol.UnsafeCommand):
            deck._send(packet)
    assert deck.transport.written == []
    # proven commands and image data still go through
    deck.initialise()
    deck.set_key_jpeg(1, b"\xff\xd8" + b"\x00" * 3000)
    deck.commit()
    assert len(deck.transport.written) > 4
    # a deck opened for experiments is the only exception
    loose = Deck(FakeTransport(), SOOMFON_XF_CN001, allow_experimental=True)
    loose._send(protocol.pad(protocol.PREFIX + b"LOG", 1024))
    assert len(loose.transport.written) == 1


def test_position_image_uses_firmware_numbering(deck):
    """<summary>
    Pins the grid position to key number conversion at the point it reaches
    the wire.
    </summary>
    <remarks>
    The firmware numbers keys down each column starting from the right hand
    side, so the top left key, which everyone thinks of as the first, is
    number 13. Reading the number back out of the assembled BAT header is
    the point: a conversion that is correct in the layout module but applied
    in the wrong place, or applied twice, would still pass a test written
    against the helper. Getting this wrong does not raise anywhere. Every
    picture simply lands on the wrong key and every press is attributed to
    the wrong one.
    </remarks>
    """
    deck.set_position_image(0, 0, images.solid((0, 0, 0), 10))  # top left is key 13
    header = deck.transport.written[0]
    assert header[6:9] == b"BAT"
    assert header[13] == 13


def test_short_write_is_an_error(deck):
    """<summary>
    Pins a transport that accepts only part of a packet as a raise, not
    something to retry or ignore.
    </summary>
    <remarks>
    A partial write leaves the deck part way through a report and waiting,
    and any further packet is then read against the wrong offset, so
    carrying on would put the device into a state nobody has captured.
    Raising stops the sequence dead and lets the run loop reopen the device
    from a known point. Sending the remainder is specifically not the
    behaviour wanted here.
    </remarks>
    """
    deck.transport.write = lambda data: 10
    with pytest.raises(OSError):
        deck.set_brightness(10)


def test_read_event_decodes_and_times_out():
    """<summary>
    Pins key press decoding, and pins an idle poll as None rather than an
    exception.
    </summary>
    <remarks>
    Both halves matter. The press and the release of the same key must be
    told apart, since holds and repeats key off the release and a decoder
    that reported everything as a press would leave keys stuck down. The
    None is just as important: the reader polls constantly and a timeout is
    the normal outcome of most polls, so a raise there would flood the log
    and churn the run loop for no reason at all.
    </remarks>
    """
    transport = FakeTransport(incoming=[_report(7, 1), _report(7, 0)])
    deck = Deck(transport, SOOMFON_XF_CN001)
    assert deck.read_event() == protocol.KeyEvent(7, True)
    assert deck.read_event() == protocol.KeyEvent(7, False)
    assert deck.read_event() is None


def test_read_error_triggers_reopen(monkeypatch):
    """<summary>
    Pins a read error as a reopen and carry on, not as a fatal fault.
    </summary>
    <remarks>
    This exists because of a real Windows quirk: hidapi raises one transient
    read error per session while the deck is still enumerated and perfectly
    healthy. Treating that as fatal would drop the deck once, every session,
    on that platform. The last assertion is the one with teeth: after the
    reopen the very next read must decode a genuine key event, proving the
    reader recovered rather than merely survived. The sleep is patched out
    so the backoff does not slow the suite.
    </remarks>
    """
    monkeypatch.setattr("deckplate.device.time.sleep", lambda s: None)
    transport = FakeTransport(incoming=[_report(3, 1)], fail_reads=1)
    deck = Deck(transport, SOOMFON_XF_CN001)
    assert deck.read_event() is None
    assert transport.reopened == 1
    assert deck.read_event() == protocol.KeyEvent(3, True)


def test_context_manager_closes(deck):
    """<summary>
    Pins the deck releasing the device when used as a context manager.
    </summary>
    <remarks>
    A deck left open holds the HID interface, so the next run of the daemon,
    or the official app, finds the device busy with no clue as to who has
    it. That is the same symptom the conflict detection exists to explain,
    which makes it a confusing failure to chase.
    </remarks>
    """
    with deck:
        pass
    assert deck.transport.closed


def test_open_returns_a_fake_deck_only_when_the_code_flag_is_on(monkeypatch):
    """<summary>
    Pins the development fake as something only the in code flag can
    produce, and pins the safety gate as applying to it just the same.
    </summary>
    <remarks>
    The closing gate assertion in this test must never be weakened. It is
    the second of the two that hold the hardware gates shut: the fake is the
    real Deck class over a memory transport, so an unproven command must be
    refused there exactly as it is on hardware. If the fake were allowed to
    accept anything, it would become a place to develop a command that has
    never been captured, and that code would then be run against a real deck
    believing it was tested.

    The rest pins the flag itself. With it off, no deck present is an error
    rather than a silent fall back to a fake that hides a disconnect, which
    is how the daemon is shipped. Asking for a specific vendor and product
    id never gets the fake even with the flag on, because a caller naming a
    real device wants that device or nothing. There is deliberately no
    configuration key, environment variable or command line flag for any of
    this, so a shipped build cannot reach the fake by accident.
    </remarks>
    """
    from deckplate import device

    monkeypatch.setattr(device, "enumerate_known", lambda: [])
    monkeypatch.setattr(device, "FAKE_DECK_WHEN_ABSENT", False)
    with pytest.raises(device.DeviceNotFound):
        Deck.open()
    monkeypatch.setattr(device, "FAKE_DECK_WHEN_ABSENT", True)
    fake = Deck.open()
    assert fake.is_fake and fake.spec.name.startswith("Fake deck")
    with pytest.raises(device.DeviceNotFound):
        Deck.open(0x1500, 0x3003)  # asking for a specific deck never gets the fake
    # it is the real Deck over a memory transport, so the protocol and the gate still apply
    fake.initialise()
    fake.set_position_image(0, 0, images.numbered_tile(1, fake.image_size))
    fake.commit()
    assert fake.transport.packets > 3 and fake.transport.last_command == b"STP"
    with pytest.raises(protocol.UnsafeCommand):
        fake._send(protocol.pad(protocol.PREFIX + b"LOG", 1024))
    assert fake.read_event(1) is None
    real = Deck(FakeTransport(), SOOMFON_XF_CN001)
    assert not real.is_fake
