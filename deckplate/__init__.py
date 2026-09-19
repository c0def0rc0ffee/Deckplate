"""<summary>
Deckplate: a small driver and controller for the Soomfon Stream Controller.
</summary>
<remarks>
The deck is a Mirabox Stream Dock 293S family device. Everything here was
confirmed against a real XF-CN001 unit (USB 1500:3003) on 7 September 2026.

The core modules, in the order one leads to the next:

    protocol  the wire format, pure functions, no USB
    layout    physical key positions to device key numbers and back
    images    turning pictures into what the deck expects
    device    the hidapi transport and the Deck class that ties it together
    cli       the command line entry point

Importing this package deliberately pulls in nothing else: hidapi, Pillow and
the rest are imported by the module that needs them, so the version can be
read on a machine with none of them installed. That is what lets the build
and the packaging checks ask the source what version it is without standing
up the whole daemon.
</remarks>
"""

# Stamped by the build from the VERSION file, which is the single source of
# truth for all three of VERSION, pyproject.toml and this line. Editing it
# here does nothing lasting: the next build writes it back from VERSION.
__version__ = "1.0.14"
