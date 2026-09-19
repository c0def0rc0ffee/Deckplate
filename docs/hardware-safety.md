# Hardware Safety: How the Deck Is Treated

Written 10 September 2026 after a Soomfon XF-CN001 (Mirabox HSV293S V3P1
board) was disabled by hand sent experiments. This document is binding for
every future session on this project. Read it before touching a deck.

## What happened

- The daemon had run for days on the deck with seven commands and never
  caused a problem: DIS, CONNECT, LIG, CLE, HAN, BAT and STP.
- Chasing a cosmetic white surround that appeared after a USB drop, test
  scripts sent commands outside that set straight to the deck: LOG (which
  turned out to be the boot logo, 854 by 480, under 512 KB) with pictures of
  the wrong size and form, and MOD (a mode change) with a guessed value.
- The deck stored a mangled boot logo, then stopped bringing up USB at all.
  It still boots and draws the bad logo, but it cannot be reached, no button
  enters the chip's recovery mode, and no firmware image is public.
- The deck was lost. A replacement was ordered.

## The rule

1. Only the daemon talks to a deck. No hand written test scripts against a
   deck that matters, ever.
2. The daemon sends only the seven proven commands. Two gates enforce it:
   `protocol.command()` refuses to build anything else without
   `experimental=True`, and `Deck._send()` refuses to write anything else
   unless the deck was opened with `allow_experimental=True`. The daemon
   sets neither. Tests cover both gates.
3. Nothing new goes to the deck on a guess. A new command is added only as
   a byte for byte copy of what the official app was captured sending, with
   the same picture sizes and limits, and the comparison is shown to Rob
   before it ships.
4. Never send LOG, MOD, BGPIC, BGCLE, LBLIG, SETLB, DELED, QUCMD, LLUM, LMOD,
   COLOR, CPOS or CHECK from anything other than a deliberate, agreed test on
   a deck that can be spared. There is no such deck at present.

## How to learn what the official app does, safely

The official app does everything the deck can do and does it to thousands
of decks a day. The way to learn any of it is to listen, not to send.

1. Install USBPcap on the Windows machine (bundled in the Wireshark
   installer as a tick box, or from usbpcap.org). Admin install, one
   reboot.
2. With the official app running, start a capture from the command line
   with USBPcapCMD on the root hub the deck is on, writing a pcap file.
3. Plug the deck in and let the app connect: this records the connection
   sequence, including whatever paints the background behind the keys.
4. Use the app's boot logo feature with a chosen picture: this records the
   exact form the deck expects for LOG.
5. Unplug and replug the deck with the app running: this records how the
   app restores the deck after a drop.
6. Stop the capture. Decode the pcap with Python (USBPcap headers, then the
   HID output reports), list the packets by command name, and keep the file
   with the project notes.

Only after that, and only for something worth having, a command may be
added to the daemon as an exact copy of the captured packets, behind the
gates, with tests that pin the bytes.

## The replacement deck

- First run: the built daemon only, with the official app closed. A day of
  normal use, then a deliberate unplug and replug to confirm the reconnect.
- Keep the official app installed. It is the only thing that repaints the
  surround if a USB drop ever leaves it white, and it is the reference for
  every capture.
- The white surround after a USB drop is cosmetic and is not worth any
  risk. It waits for the capture above or stays as it is.

## The old deck

- Board marking HSV293S-V3P1. Chip is an ArtInChip part; its boot ROM USB
  recovery mode enumerates as VID 33C3 PID 6677 and the tool in the official
  app folder, FirmwareUpgradeTool.exe, is the ArtInChip upgrade tool.
- No button enters recovery. Two unlabelled six pad headers are inside; a
  3.3 volt serial console on the UART one would show the boot log and may
  allow entering recovery from the boot loader. Optional project only.
- Firmware image and recovery procedure: ask Mirabox (service@key123.vip)
  or SOOMFON (support@soomfon.com), quoting the board marking, the serial
  8730DB783520 and firmware V3.CN001.02.010.
