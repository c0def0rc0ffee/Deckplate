"""<summary>
The audio module: parsing what pactl and PowerShell print, choosing an
output, and the exact commands the Linux backend runs.
</summary>
<remarks>
Nothing here runs a process or touches the machine's sound. The backend is
built over a recorder standing in for subprocess.run, so every test asserts
the command line that would have run, which is the whole contract with
pactl: a wrong flag there is a key that silently does nothing.
</remarks>
"""

import subprocess

import pytest

from deckplate import audio


class Runner:
    """<summary>Records every command and answers from a queue of outputs.</summary>"""

    def __init__(self, *answers):
        """<summary>Queue the standard output to give back, in order.</summary>"""
        self.calls = []
        self.answers = list(answers)

    def __call__(self, args, **kwargs):
        """<summary>Record the call and answer with the next queued output.</summary>"""
        self.calls.append(args)
        text = self.answers.pop(0) if self.answers else ""
        if isinstance(text, Exception):
            raise text
        return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")


def test_parsing_pactl_output():
    """<summary>
    The JSON sink list, the short list, the mute line and the volume line
    each parse to what a key needs.
    </summary>
    <remarks>
    The short list is the fallback for a pactl too old to print JSON, so it
    has to give the same shape with the name doing double duty. The mute line
    is matched on the word rather than the whole line because pactl's
    spacing has varied between versions.
    </remarks>
    """
    sinks = audio.parse_sinks_json('[{"name": "alsa_output.pci", "description": "Built-in Audio"},'
                                   ' {"name": "bluez_output.aa", "description": "Headphones"}, 5]')
    assert sinks == [audio.Output("alsa_output.pci", "Built-in Audio"), audio.Output("bluez_output.aa", "Headphones")]
    assert audio.parse_short_sinks("0\talsa_output.pci\tmodule\ts16le\tRUNNING\n1\tbluez_output.aa\tm\ts\tIDLE\n") == [
        audio.Output("alsa_output.pci", "alsa_output.pci"), audio.Output("bluez_output.aa", "bluez_output.aa")]
    assert audio.parse_mute("Mute: yes") is True
    assert audio.parse_mute("Mute: no") is False
    assert audio.parse_mute("") is None
    assert audio.parse_volume("Volume: front-left: 32768 /  50% / -18.06 dB,   front-right: 32768 /  50%") == 50
    assert audio.parse_volume("nothing") is None
    assert audio.parse_device_json('{"ID": "{0.0.0}", "Name": "Speakers"}') == [audio.Output("{0.0.0}", "Speakers")]
    assert audio.parse_device_json("") == []


def test_pick_output_by_pattern_and_by_cycling():
    """<summary>
    A pattern picks the first output it is found in, by id or name, ignoring
    case, and cycling picks the one after the current, wrapping round.
    </summary>
    <remarks>
    Cycling from an unknown current output starts at the first rather than
    doing nothing, so the key works before the daemon has ever asked what is
    in use. A single output cannot be cycled to itself, which would otherwise
    move every playing stream for no change.
    </remarks>
    """
    outputs = [audio.Output("alsa_output.pci", "Built-in Audio"), audio.Output("bluez_output.aa", "WH-1000 Headphones")]
    assert audio.pick_output(outputs, "headphones") == outputs[1]
    assert audio.pick_output(outputs, "ALSA") == outputs[0]
    assert audio.pick_output(outputs, "nothing") is None
    assert audio.pick_output(outputs, "(") is None
    assert audio.pick_output(outputs, cycle=True, current="alsa_output.pci") == outputs[1]
    assert audio.pick_output(outputs, cycle=True, current="bluez_output.aa") == outputs[0]
    assert audio.pick_output(outputs, cycle=True, current=None) == outputs[0]
    assert audio.pick_output(outputs[:1], cycle=True, current="alsa_output.pci") is None
    assert audio.pick_output([], "x") is None


def test_pactl_backend_runs_the_right_commands():
    """<summary>
    Each backend call becomes the pactl line it should, against the default
    sink, and switching output moves the playing streams too.
    </summary>
    <remarks>
    The default sink alias is the contract: a key that named a fixed sink
    would stop working the moment the user switched outputs. The stream move
    is what makes a switch audible on a film already playing.
    </remarks>
    """
    run = Runner("", "", "", "Mute: yes", "Volume: front-left: 0 / 40%", "", "12\tx\n13\ty\n", "", "")
    backend = audio.PactlBackend(run)
    backend.set_volume(50)
    backend.change_volume(-5)
    backend.set_mute("toggle")
    assert backend.is_muted() is True
    assert backend.volume() == 40
    backend.set_output("bluez_output.aa")
    assert run.calls[:5] == [
        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "50%"],
        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-5%"],
        ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
        ["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
        ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
    ]
    assert run.calls[5:] == [
        ["pactl", "set-default-sink", "bluez_output.aa"],
        ["pactl", "list", "short", "sink-inputs"],
        ["pactl", "move-sink-input", "12", "bluez_output.aa"],
        ["pactl", "move-sink-input", "13", "bluez_output.aa"],
    ]


def test_pactl_backend_falls_back_to_the_short_list_and_names_a_missing_tool():
    """<summary>
    A pactl that cannot print JSON still lists outputs through the short
    form, and a missing pactl raises an AudioError naming the package.
    </summary>
    """
    run = Runner(subprocess.CalledProcessError(1, ["pactl"], stderr="unknown option"), "0\tsink_a\tm\n")
    assert audio.PactlBackend(run).outputs() == [audio.Output("sink_a", "sink_a")]
    missing = Runner(FileNotFoundError())
    with pytest.raises(audio.AudioError, match="pactl"):
        audio.PactlBackend(missing).volume()
