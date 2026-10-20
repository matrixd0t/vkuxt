from __future__ import annotations

import asyncio

from examples.tictactoe import (
    AskJoinCodeInput,
    BackToMenuInput,
    CloseInput,
    CreateLobbyInput,
    GameScreenState,
    GameState,
    LobbyScreenState,
    LobbyState,
    MenuState,
    MoveInput,
    Participant,
    SurrenderInput,
    WaitingCodeState,
    register_handlers,
)
from tests.test_vk_adapter import FakeVK, add_vk_adapter

from vkuxt import App, Input, JSONFileStorage, MemoryStorage, VKMessageMetadata


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


def test_tictactoe_workflow_dispatches_by_screen_state() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk, dm_latest_interface=False)
        register_handlers(app)

        # Игрок 1 открывает меню
        p1_msg = VKMessageMetadata(
            event_type="message_new",
            group_id=71,
            peer_id=101,
            user_id=1,
        )
        await adapter.dispatch_input(Input(p1_msg, text="меню"))
        await app.wait_background()
        p1_handle = await adapter.latest_interface(101)
        assert p1_handle is not None
        assert await app.get_state(p1_handle.state_key(), MenuState) == MenuState(uid=1)

        # Игрок 1 создаёт лобби
        p1_click = VKMessageMetadata(
            event_type="message_event",
            group_id=71,
            peer_id=101,
            user_id=1,
            event_id="ev-1",
        )
        p1_click._interface_cmid = p1_handle.id.cmid
        await adapter.dispatch_input(Input(p1_click, data=CreateLobbyInput(uid=1)))
        await app.wait_background()
        lobby_state = await app.get_state(p1_handle.state_key(), LobbyScreenState)
        assert lobby_state is not None
        code = lobby_state.code

        # Игрок 2 пытается отправить код без перехода в WaitingCodeState — игнорируется
        p2_msg = VKMessageMetadata(
            event_type="message_new",
            group_id=71,
            peer_id=202,
            user_id=2,
        )
        await adapter.dispatch_input(Input(p2_msg, text=code))
        await app.wait_background()
        assert await adapter.latest_interface(202) is None

        # Игрок 2 открывает меню и нажимает «Подключиться к игре»
        await adapter.dispatch_input(Input(p2_msg, text="начать"))
        await app.wait_background()
        p2_handle = await adapter.latest_interface(202)
        assert p2_handle is not None

        p2_ask = VKMessageMetadata(
            event_type="message_event",
            group_id=71,
            peer_id=202,
            user_id=2,
            event_id="ev-2",
        )
        p2_ask._interface_cmid = p2_handle.id.cmid
        await adapter.dispatch_input(Input(p2_ask, data=AskJoinCodeInput(uid=2)))
        await app.wait_background()
        assert await app.get_state(
            p2_handle.state_key(),
            WaitingCodeState,
        ) == WaitingCodeState(uid=2)

        # Игрок 2 вводит код ответом на сообщение
        p2_reply = VKMessageMetadata(
            event_type="message_new",
            group_id=71,
            peer_id=202,
            user_id=2,
        )
        p2_reply._message_cmid = 55
        p2_reply._reply_cmid = p2_handle.id.cmid
        await adapter.dispatch_input(Input(p2_reply, text=code))
        await app.wait_background()

        p1_game_handle = await adapter.latest_interface(101)
        p2_game_handle = await adapter.latest_interface(202)
        assert p1_game_handle is not None
        assert p2_game_handle is not None
        p1_screen = await app.get_state(p1_game_handle.state_key(), GameScreenState)
        p2_screen = await app.get_state(p2_game_handle.state_key(), GameScreenState)
        assert p1_screen is not None
        assert p2_screen is not None
        assert p1_screen.game_id == p2_screen.game_id
        assert p1_screen.seat == 0
        assert p2_screen.seat == 1

        # Ход первого игрока (или того, чей сейчас ход)
        game = await app.get_state(
            {"app": "ttt", "kind": "game", "group_id": 71, "game_id": p1_screen.game_id},
            GameState,
        )
        assert game is not None
        active_seat = game.turn
        active_handle = p1_game_handle if active_seat == 0 else p2_game_handle
        active_user = 1 if active_seat == 0 else 2
        active_peer = 101 if active_seat == 0 else 202
        move_event = VKMessageMetadata(
            event_type="message_event",
            group_id=71,
            peer_id=active_peer,
            user_id=active_user,
            event_id="ev-move",
        )
        move_event._interface_cmid = active_handle.id.cmid
        await adapter.dispatch_input(
            Input(
                move_event,
                data=MoveInput(
                    game_id=p1_screen.game_id,
                    seat=active_seat,
                    cell=4,
                ),
            )
        )
        await app.wait_background()

        # Игрок 2 сдаётся -> партия завершается
        p2_surrender = VKMessageMetadata(
            event_type="message_event",
            group_id=71,
            peer_id=202,
            user_id=2,
            event_id="ev-3",
        )
        p2_surrender._interface_cmid = p2_game_handle.id.cmid
        await adapter.dispatch_input(
            Input(
                p2_surrender,
                data=SurrenderInput(game_id=p2_screen.game_id, seat=1),
            )
        )
        await app.wait_background()

        # Игрок 1 возвращается в меню и закрывает его
        p1_back = VKMessageMetadata(
            event_type="message_event",
            group_id=71,
            peer_id=101,
            user_id=1,
            event_id="ev-4",
        )
        p1_back._interface_cmid = p1_game_handle.id.cmid
        await adapter.dispatch_input(
            Input(
                p1_back,
                data=BackToMenuInput(game_id=p1_screen.game_id, seat=0),
            )
        )
        await app.wait_background()
        assert await app.get_state(p1_game_handle.state_key(), MenuState) == MenuState(
            uid=1
        )

        p1_close = VKMessageMetadata(
            event_type="message_event",
            group_id=71,
            peer_id=101,
            user_id=1,
            event_id="ev-5",
        )
        p1_close._interface_cmid = p1_game_handle.id.cmid
        await adapter.dispatch_input(
            Input(
                p1_close,
                data=CloseInput(uid=1),
            )
        )
        await app.wait_background()
        assert await app.get_state(p1_game_handle.state_key()) is None
        await app.close()

    asyncio.run(scenario())
