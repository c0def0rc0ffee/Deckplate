"""<summary>
The Kodi shorthand and what a kodi key actually sends, through a runner
whose JSON door records every request.
</summary>
"""

import pytest

from deckplate import kodi
from deckplate.actions import ActionRunner
from deckplate.config import Action, KodiSettings


class Context:
    """<summary>A deck context that does nothing, for a runner under test.</summary>"""

    def switch_page(self, target):
        """<summary>Ignored.</summary>"""

    def set_brightness(self, value=None, delta=None):
        """<summary>Ignored.</summary>"""

    def sleep_deck(self):
        """<summary>Ignored.</summary>"""


def test_shorthand_resolves_and_raw_methods_pass_through():
    """<summary>Every shorthand maps to a method, and a raw method keeps its params.</summary>"""
    assert kodi.resolve({"do": "play_pause"}) == ("Player.PlayPause", {}, True)
    assert kodi.resolve({"do": "up"}) == ("Input.Up", {}, False)
    assert kodi.resolve({"method": "GUI.ShowNotification", "params": {"title": "a", "message": "b"}}) == \
        ("GUI.ShowNotification", {"title": "a", "message": "b"}, False)
    with pytest.raises(ValueError):
        kodi.resolve({"do": "dance"})
    with pytest.raises(ValueError):
        kodi.resolve({})
    assert kodi.payload("Input.Up") == {"jsonrpc": "2.0", "id": 1, "method": "Input.Up"}
    assert kodi.first_player({"result": [{"playerid": 1, "type": "video"}]}) == 1
    assert kodi.first_player({"result": []}) is None
    assert kodi.first_player({"error": "x"}) is None


def make_runner(replies, tmp_path=None, token=None):
    """<summary>A runner whose JSON door answers from a list and records what was sent.</summary>
    <returns>The runner and the list of (url, headers, body) it sent.</returns>"""
    sent = []
    lines = []

    def send_json(url, headers, body, timeout):
        sent.append((url, headers, body, timeout))
        return replies.pop(0) if replies else {}

    settings = KodiSettings(url="http://player.test:8080/jsonrpc", token_file=token, timeout_s=3)
    runner = ActionRunner(Context(), send_json=send_json, kodi=settings, log=lines.append,
                          read_token=lambda path: "Basic abc")
    return runner, sent, lines


def test_a_player_action_finds_the_active_player_first():
    """<summary>play_pause asks for the active players, then acts on the first.</summary>"""
    runner, sent, _lines = make_runner([{"result": [{"playerid": 1, "type": "video"}]}, {"result": "OK"}])
    assert runner.run(Action("kodi", {"do": "play_pause"}))
    assert [item[2]["method"] for item in sent] == ["Player.GetActivePlayers", "Player.PlayPause"]
    assert sent[1][2]["params"] == {"playerid": 1}
    assert sent[1][0] == "http://player.test:8080/jsonrpc" and sent[1][3] == 3


def test_nothing_playing_is_logged_and_nothing_else_is_sent():
    """<summary>With no active player the action stops after the first request and says why.</summary>"""
    runner, sent, lines = make_runner([{"result": []}])
    assert runner.run(Action("kodi", {"do": "stop"}))
    assert len(sent) == 1
    assert any("nothing is playing" in line for line in lines)


def test_input_actions_send_once_and_the_token_file_becomes_the_header():
    """<summary>A remote key sends one request, with the [kodi] credentials on it.</summary>"""
    runner, sent, _lines = make_runner([{"result": "OK"}], token="~/.deckplate-tokens/kodi")
    assert runner.run(Action("kodi", {"do": "volume_up"}))
    assert len(sent) == 1
    assert sent[0][1] == {"Authorization": "Basic abc"}
    assert sent[0][2]["params"] == {"volume": "increment"}


def test_a_bad_shorthand_fails_the_action_without_raising():
    """<summary>The runner swallows the error and reports False, as for every action.</summary>"""
    runner, sent, lines = make_runner([])
    assert runner.run(Action("kodi", {"do": "nope"})) is False
    assert sent == [] and any("failed" in line for line in lines)
