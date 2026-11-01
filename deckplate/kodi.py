"""<summary>
Kodi's JSON RPC, as far as a deck key needs it: the shorthand names a key
can carry and the request each one becomes.
</summary>
<remarks>
A kodi action names what to do in plain words, play_pause, stop, up, select
and so on, and this module turns the word into the method and parameters
Kodi wants. A key may also give a raw ``method`` and ``params`` for anything
not listed. The address and the optional credentials live in the config's
[kodi] table, shared by every kodi key, so a player that moves is edited in
one place.

The player actions need a player id that only Kodi knows, so the runner asks
Player.GetActivePlayers first and uses the first one. With nothing playing
those actions do nothing and say so in the log.

Pure: nothing here sends anything. The runner sends through its injectable
request function, which is how the tests see exactly what would go.
</remarks>
"""

from __future__ import annotations

from typing import Any

# Shorthand name: (method, params, needs a player id)
SHORTHAND: dict[str, tuple[str, dict[str, Any], bool]] = {
    "play_pause": ("Player.PlayPause", {}, True),
    "stop": ("Player.Stop", {}, True),
    "next": ("Player.GoTo", {"to": "next"}, True),
    "previous": ("Player.GoTo", {"to": "previous"}, True),
    "up": ("Input.Up", {}, False),
    "down": ("Input.Down", {}, False),
    "left": ("Input.Left", {}, False),
    "right": ("Input.Right", {}, False),
    "select": ("Input.Select", {}, False),
    "back": ("Input.Back", {}, False),
    "home": ("Input.Home", {}, False),
    "info": ("Input.Info", {}, False),
    "context": ("Input.ContextMenu", {}, False),
    "osd": ("Input.ShowOSD", {}, False),
    "volume_up": ("Application.SetVolume", {"volume": "increment"}, False),
    "volume_down": ("Application.SetVolume", {"volume": "decrement"}, False),
    "mute": ("Application.SetMute", {"mute": "toggle"}, False),
}


def payload(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    """<summary>One JSON RPC request body.</summary>
    <param name="method">The Kodi method, such as Player.PlayPause.</param>
    <param name="params">Its parameters, or None for none.</param>
    <param name="request_id">The id Kodi echoes back.</param>
    <returns>The body as a dict, ready to be sent as JSON.</returns>"""
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        body["params"] = params
    return body


def resolve(action_params: dict) -> tuple[str, dict, bool]:
    """<summary>What a kodi action's params ask for.</summary>
    <param name="action_params">``do`` for a shorthand, or ``method`` with
    optional ``params``.</param>
    <returns>(method, params, needs_player): the request to make and whether
    a player id must be found first.</returns>
    <exception cref="ValueError">Neither a known shorthand nor a method.</exception>"""
    do = action_params.get("do")
    if do:
        if do not in SHORTHAND:
            raise ValueError(f"unknown kodi action '{do}'")
        method, params, needs_player = SHORTHAND[do]
        return method, dict(params), needs_player
    method = action_params.get("method")
    if not method:
        raise ValueError("kodi action needs 'do' or 'method'")
    return method, dict(action_params.get("params") or {}), False


def first_player(reply: Any) -> int | None:
    """<summary>The id of the first active player in a GetActivePlayers reply.</summary>
    <param name="reply">The decoded reply body.</param>
    <returns>The player id, or None when nothing is playing.</returns>"""
    try:
        players = reply["result"]
        return int(players[0]["playerid"]) if players else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None
