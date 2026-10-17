from __future__ import annotations

import asyncio

from examples.tictactoe import GameState, LobbyState, Participant

from vkuxt import App, JSONFileStorage


def test_example_business_state_survives_restart(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "state.json"
        lobby_key = {
            "app": "ttt",
            "kind": "lobby",
            "group_id": 71,
            "code": "A1B2C3",
        }
        game_key = {
            "app": "ttt",
            "kind": "game",
            "group_id": 71,
            "game_id": "game-1",
        }
        owner = Participant(user_id=1, peer_id=100)
        joiner = Participant(user_id=2, peer_id=200)
        lobby = LobbyState(owner=owner)
        game = GameState(
            status="finished",
            game_id="game-1",
            players=[owner, joiner],
            board=["X", "O", "X", "O", "X", "O", None, None, None],
            turn=0,
            winner=0,
            finish_reason="line",
        )

        app = App(JSONFileStorage(path))
        await app.set_state(lobby_key, lobby, ttl=60)
        await app.set_state(game_key, game, ttl=60)
        await app.close()

        restored = App(JSONFileStorage(path))
        assert await restored.get_state(lobby_key, LobbyState) == lobby
        assert await restored.get_state(game_key, GameState) == game
        await restored.close()

    asyncio.run(scenario())
