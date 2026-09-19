"""<summary>
The machine's sound: volume, mute and which output is in use.
</summary>
<remarks>
Two backends behind one small interface. Linux talks to PulseAudio or
PipeWire through ``pactl``, which is on every Mint install; Windows uses the
pycaw library for volume and mute, and the AudioDeviceCmdlets PowerShell
module for switching outputs, and says plainly which of the two is missing
rather than failing quietly.

Everything that parses text is a module level function taking the text, so
the tests feed it captured output and never run a process. Everything that
runs a process goes through a ``run`` callable the backend was built with,
which the tests replace with a recorder.

Nothing here touches the deck. A volume key is a PC side action like a
hotkey, and this module is what the action runner calls for it.
</remarks>
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

MUTE_MODES = ("toggle", "on", "off")


class AudioError(RuntimeError):
    """<summary>
    The sound system could not be driven, with a message saying what to install.
    </summary>
    <remarks>
    Raised for a missing tool or library, not for a bad request: a volume out
    of range is refused by the config layer long before this. The message is
    written for the log, so it names the package rather than the exception.
    </remarks>
    """

    pass


@dataclass(frozen=True)
class Output:
    """<summary>
    One place sound can go: a PulseAudio sink or a Windows playback device.
    </summary>
    <param name="id">What the system calls it: a sink name on Linux, a device
    id on Windows. Passed back untouched when switching.</param>
    <param name="name">The human readable description, for matching against
    what the user wrote in the config.</param>
    <remarks>
    Matching is done against both fields, because a user may write either
    the description they see in the sound settings ("Headphones") or the
    sink name they got from pactl.
    </remarks>
    """

    id: str
    name: str


def parse_sinks_json(text: str) -> list[Output]:
    """<summary>
    The outputs in ``pactl -f json list sinks`` output.
    </summary>
    <param name="text">The JSON the command printed.</param>
    <returns>One Output per sink, in the order pactl listed them.</returns>
    <remarks>
    Only the name and the description are read. Anything unexpected in the
    document, a missing key or a sink that is not an object, is skipped
    rather than raised, so one odd entry does not hide the rest.
    </remarks>

    <exception cref="ValueError">The text is not JSON at all.</exception>"""
    data = json.loads(text)
    outputs = []
    for entry in data if isinstance(data, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            outputs.append(Output(entry["name"], str(entry.get("description") or entry["name"])))
    return outputs


def parse_short_sinks(text: str) -> list[Output]:
    """<summary>
    The outputs in ``pactl list short sinks`` output, for a pactl too old for JSON.
    </summary>
    <param name="text">The tab separated lines the command printed.</param>
    <returns>One Output per line, with the name standing in for the
    description since the short form has none.</returns>
    """
    outputs = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1]:
            outputs.append(Output(parts[1], parts[1]))
    return outputs


def parse_mute(text: str) -> bool | None:
    """<summary>
    Whether ``pactl get-sink-mute`` said the sink is muted.
    </summary>
    <param name="text">The line printed, such as ``Mute: yes``.</param>
    <returns>True or False, or None when the text says neither.</returns>
    """
    match = re.search(r"Mute:\s*(yes|no)", text)
    if match is None:
        return None
    return match.group(1) == "yes"


def parse_volume(text: str) -> int | None:
    """<summary>
    The first percentage in ``pactl get-sink-volume`` output.
    </summary>
    <param name="text">The two channel line pactl prints.</param>
    <returns>The left channel's percentage, or None when none was found.</returns>
    <remarks>Both channels are set together by this module, so the first
    one is the volume as far as a deck key is concerned.</remarks>
    """
    match = re.search(r"(\d+)%", text)
    return int(match.group(1)) if match else None


def pick_output(outputs: list[Output], pattern: str | None = None, cycle: bool = False,
                current: str | None = None) -> Output | None:
    """<summary>
    Which output a key wants: the first matching a pattern, or the next one round.
    </summary>
    <param name="outputs">Every output the system has, in its order.</param>
    <param name="pattern">A regular expression searched, ignoring case, in
    each output's id and name. Ignored when cycling.</param>
    <param name="cycle">True to pick the output after the current one,
    wrapping round at the end.</param>
    <param name="current">The id of the output in use now, for cycling.</param>
    <returns>The output to switch to, or None when nothing matched or there
    is nothing to cycle to.</returns>
    <remarks>
    Cycling from an unknown current output starts at the first, so a key
    pressed before anything is known still does something. A pattern that
    will not compile matches nothing rather than raising, the same rule the
    per application pages follow.
    </remarks>
    """
    if not outputs:
        return None
    if cycle:
        ids = [output.id for output in outputs]
        index = ids.index(current) if current in ids else -1
        chosen = outputs[(index + 1) % len(outputs)]
        return None if chosen.id == current and len(outputs) == 1 else chosen
    if not pattern:
        return None
    try:
        expression = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None
    for output in outputs:
        if expression.search(output.id) or expression.search(output.name):
            return output
    return None


class PactlBackend:
    """<summary>
    Linux sound through the pactl command, against the default sink.
    </summary>
    <remarks>
    Every call is one short process. ``@DEFAULT_SINK@`` is used throughout so
    the key follows whatever output the user has chosen, including one this
    module switched to a moment ago.

    Switching output moves the streams that are already playing as well as
    setting the default, because setting the default alone leaves a playing
    film on the old speakers, which is not what anyone pressing the key
    meant.
    </remarks>
    """

    def __init__(self, run: Callable = subprocess.run) -> None:
        """<summary>Build over a runner, the real subprocess.run unless a test says otherwise.</summary>
        <param name="run">Called with the argument list and keyword arguments
        subprocess.run takes.</param>"""
        self._run = run

    def _pactl(self, *args: str) -> str:
        """<summary>Run one pactl command and hand back what it printed.</summary>
        <param name="args">The arguments after ``pactl``.</param>
        <returns>Standard output, stripped.</returns>
        <exception cref="AudioError">pactl is missing or refused the command.</exception>"""
        try:
            result = self._run(["pactl", *args], capture_output=True, text=True, timeout=5, check=True)
        except FileNotFoundError as err:
            raise AudioError("pactl is not installed (apt install pulseaudio-utils)") from err
        except subprocess.CalledProcessError as err:
            detail = (err.stderr or "").strip() or f"exit {err.returncode}"
            raise AudioError(f"pactl {args[0]} failed: {detail}") from err
        return (result.stdout or "").strip()

    def set_volume(self, percent: int) -> None:
        """<summary>Set the default output to a level.</summary>
        <param name="percent">0 to 150. Above 100 is allowed, as pactl allows it.</param>"""
        self._pactl("set-sink-volume", "@DEFAULT_SINK@", f"{percent}%")

    def change_volume(self, delta: int) -> None:
        """<summary>Step the default output's level up or down.</summary>
        <param name="delta">Percentage points, negative to go down.</param>"""
        self._pactl("set-sink-volume", "@DEFAULT_SINK@", f"{delta:+d}%")

    def set_mute(self, mode: str) -> None:
        """<summary>Mute, unmute or flip the default output.</summary>
        <param name="mode">One of MUTE_MODES.</param>"""
        value = {"toggle": "toggle", "on": "1", "off": "0"}[mode]
        self._pactl("set-sink-mute", "@DEFAULT_SINK@", value)

    def is_muted(self) -> bool | None:
        """<summary>Whether the default output is muted now.</summary>
        <returns>True or False, or None when pactl gave no answer.</returns>"""
        return parse_mute(self._pactl("get-sink-mute", "@DEFAULT_SINK@"))

    def volume(self) -> int | None:
        """<summary>The default output's level now.</summary>
        <returns>A percentage, or None when pactl gave no answer.</returns>"""
        return parse_volume(self._pactl("get-sink-volume", "@DEFAULT_SINK@"))

    def outputs(self) -> list[Output]:
        """<summary>Every output, JSON where pactl can do it and the short list otherwise.</summary>
        <returns>The outputs in pactl's order.</returns>"""
        try:
            return parse_sinks_json(self._pactl("-f", "json", "list", "sinks"))
        except (AudioError, ValueError):
            return parse_short_sinks(self._pactl("list", "short", "sinks"))

    def default_output(self) -> str | None:
        """<summary>The id of the output in use now.</summary>
        <returns>The sink name, or None when pactl gave nothing.</returns>"""
        return self._pactl("get-default-sink") or None

    def set_output(self, output_id: str) -> None:
        """<summary>Make an output the default and move every playing stream onto it.</summary>
        <param name="output_id">A sink name from <see cref="outputs"/>.</param>
        <remarks>A stream that refuses to move is skipped, not fatal: the
        default has already changed, which is the larger part of the job.</remarks>"""
        self._pactl("set-default-sink", output_id)
        for line in self._pactl("list", "short", "sink-inputs").splitlines():
            stream = line.split("\t")[0].strip()
            if stream.isdigit():
                try:
                    self._pactl("move-sink-input", stream, output_id)
                except AudioError:
                    continue


class WindowsBackend:
    """<summary>
    Windows sound: pycaw for the level and mute, AudioDeviceCmdlets for outputs.
    </summary>
    <remarks>
    pycaw wraps the Core Audio endpoint volume interface and is a pip
    install. Switching the default output has no public API, so it goes
    through the AudioDeviceCmdlets PowerShell module, which the user installs
    once with ``Install-Module AudioDeviceCmdlets``. Each half reports its
    own missing piece and the other half keeps working.

    The endpoint interface is built on every call rather than kept, because
    it belongs to a COM apartment and the action thread is not always the
    thread that built it.
    </remarks>
    """

    def __init__(self, run: Callable = subprocess.run) -> None:
        """<summary>Build over a runner for the PowerShell half.</summary>
        <param name="run">Called like subprocess.run; replaced in tests.</param>"""
        self._run = run

    def _endpoint(self):
        """<summary>The endpoint volume interface for the default output.</summary>
        <returns>A pycaw IAudioEndpointVolume pointer.</returns>
        <exception cref="AudioError">pycaw is not installed.</exception>"""
        try:
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except ImportError as err:
            raise AudioError("volume control on Windows needs pycaw (pip install pycaw)") from err
        device = AudioUtilities.GetSpeakers()
        interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))

    def set_volume(self, percent: int) -> None:
        """<summary>Set the default output to a level.</summary>
        <param name="percent">0 to 100; Windows has nothing above 100.</param>"""
        self._endpoint().SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100, None)

    def change_volume(self, delta: int) -> None:
        """<summary>Step the level, clamped to the 0 to 100 Windows allows.</summary>
        <param name="delta">Percentage points, negative to go down.</param>"""
        endpoint = self._endpoint()
        level = endpoint.GetMasterVolumeLevelScalar() * 100
        endpoint.SetMasterVolumeLevelScalar(max(0, min(100, level + delta)) / 100, None)

    def set_mute(self, mode: str) -> None:
        """<summary>Mute, unmute or flip the default output.</summary>
        <param name="mode">One of MUTE_MODES.</param>"""
        endpoint = self._endpoint()
        if mode == "toggle":
            endpoint.SetMute(not endpoint.GetMute(), None)
        else:
            endpoint.SetMute(mode == "on", None)

    def is_muted(self) -> bool | None:
        """<summary>Whether the default output is muted now.</summary>
        <returns>True or False.</returns>"""
        return bool(self._endpoint().GetMute())

    def volume(self) -> int | None:
        """<summary>The default output's level now.</summary>
        <returns>A percentage.</returns>"""
        return int(round(self._endpoint().GetMasterVolumeLevelScalar() * 100))

    def _powershell(self, script: str) -> str:
        """<summary>Run one PowerShell command and hand back what it printed.</summary>
        <param name="script">The command text.</param>
        <returns>Standard output, stripped.</returns>
        <exception cref="AudioError">PowerShell or the cmdlets are missing, or the command failed.</exception>"""
        try:
            result = self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, timeout=15, check=True)
        except FileNotFoundError as err:
            raise AudioError("powershell is not available") from err
        except subprocess.CalledProcessError as err:
            detail = (err.stderr or "").strip()
            if "AudioDevice" in detail and "not recognized" in detail:
                raise AudioError("switching outputs on Windows needs the AudioDeviceCmdlets module "
                                 "(Install-Module AudioDeviceCmdlets)") from err
            raise AudioError(f"powershell failed: {detail or err.returncode}") from err
        return (result.stdout or "").strip()

    def outputs(self) -> list[Output]:
        """<summary>Every playback device, through Get-AudioDevice.</summary>
        <returns>The outputs in the order Windows lists them.</returns>"""
        text = self._powershell("Get-AudioDevice -List | Where-Object Type -eq 'Playback' "
                                "| Select-Object ID, Name | ConvertTo-Json -Compress")
        return parse_device_json(text)

    def default_output(self) -> str | None:
        """<summary>The id of the playback device in use now.</summary>
        <returns>The id, or None when nothing was printed.</returns>"""
        return self._powershell("(Get-AudioDevice -Playback).ID") or None

    def set_output(self, output_id: str) -> None:
        """<summary>Make a playback device the default.</summary>
        <param name="output_id">An id from <see cref="outputs"/>.</param>"""
        safe = output_id.replace("'", "''")
        self._powershell(f"Set-AudioDevice -ID '{safe}' | Out-Null")


def parse_device_json(text: str) -> list[Output]:
    """<summary>
    The outputs in the JSON Get-AudioDevice prints, one object or a list.
    </summary>
    <param name="text">The JSON, possibly empty when there are no devices.</param>
    <returns>One Output per device.</returns>
    <remarks>PowerShell prints a single object rather than a one item list
    when there is exactly one device, so both shapes are accepted.</remarks>
    """
    if not text.strip():
        return []
    data = json.loads(text)
    entries = data if isinstance(data, list) else [data]
    outputs = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("ID"):
            outputs.append(Output(str(entry["ID"]), str(entry.get("Name") or entry["ID"])))
    return outputs


def default_backend():
    """<summary>
    The backend for this machine.
    </summary>
    <returns>A Windows backend on Windows, otherwise a pactl one.</returns>
    <remarks>
    Building either costs nothing and checks nothing: the missing tool or
    library is reported on the first call that needs it, as an AudioError
    with the install step in it, so the daemon starts the same on a machine
    without sound control and only the volume keys complain.
    </remarks>
    """
    if sys.platform == "win32":
        return WindowsBackend()
    return PactlBackend()


def available() -> bool:
    """<summary>
    Whether this machine has anything for the backend to talk to.
    </summary>
    <returns>True on Windows, and on Linux when pactl is on the path.</returns>
    """
    return sys.platform == "win32" or shutil.which("pactl") is not None
