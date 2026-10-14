from __future__ import annotations

import secrets
from typing import Any, Literal
from uuid import uuid4

from vkx import VKClient
from vkx.models import Button, Keyboard, KeyboardButton
from vkx.server import CallbackServer, create_callback_server

from vkuxt import (
    App,
    Input,
    InputData,
    InputModel,
    JSONFileStorage,
    VKMessageInterfaceId,
    VKMessageMetadata,
    VKMessagesCallbackAdapter,
)


class TicTacToeInput(InputData):
    owner_id: int | None = None


class CreateLobbyInput(TicTacToeInput):
    handler_name: Literal["ttt.create_lobby"]


class AskJoinCodeInput(TicTacToeInput):
    handler_name: Literal["ttt.ask_join_code"]


class CancelJoinInput(TicTacToeInput):
    handler_name: Literal["ttt.cancel_join"]


class CloseInput(TicTacToeInput):
    handler_name: Literal["ttt.close"]


class NoopInput(TicTacToeInput):
    handler_name: Literal["ttt.noop"]


class MoveInput(TicTacToeInput):
    handler_name: Literal["ttt.move"]
    game_id: str
    seat: int
    cell: int


class GameActionInput(TicTacToeInput):
    game_id: str
    seat: int


class SurrenderInput(GameActionInput):
    handler_name: Literal["ttt.surrender"]


class BackToMenuInput(GameActionInput):
    handler_name: Literal["ttt.back_to_menu"]


class LobbyActionInput(TicTacToeInput):
    handler_name: Literal["ttt.abandon_lobby"]
    code: str


class Participant(InputModel):
    user_id: int
    peer_id: int
    interface_id: dict[str, Any]


class TicTacToeBot:
    def __init__(self, app: App) -> None:
        self._register_handlers(app)

    def _register_handlers(self, app: App) -> None:
        @app.handler
        async def menu(app: App, input: Input) -> None:
            meta = _vk_metadata(input)
            if (
                meta is not None
                and input.text is not None
                and input.text.strip().casefold() in {"начать", "меню"}
            ):
                await self._show_menu(app, meta.peer_id, meta.user_id)

        @app.handler(background=False)
        async def create_lobby(app: App, input: Input) -> None:
            meta = _vk_metadata(input)
            data = _typed_data(input, CreateLobbyInput)
            if meta is None or data is None:
                return
            await self._create_lobby(app, meta, data)

        @app.handler(background=False)
        async def ask_join_code(app: App, input: Input) -> None:
            meta = _vk_metadata(input)
            data = _typed_data(input, AskJoinCodeInput)
            if meta is None or data is None:
                return
            await self._ask_join_code(app, meta, data)

        @app.handler
        async def join_code(app: App, input: Input) -> None:
            meta = _vk_metadata(input)
            if meta is None or input.text is None:
                return
            await self._join_code(app, meta, input.text)

        @app.handler(background=False)
        async def cancel_join(app: App, input: Input) -> None:
            meta = _vk_metadata(input)
            data = _typed_data(input, CancelJoinInput)
            if meta is None or data is None:
                return
            if data.owner_id == meta.user_id and meta.interface_id is not None:
                adapter = app.get_adapter(VKMessagesCallbackAdapter)
                await adapter.delete(meta.interface_id)

        @app.handler(background=False)
        async def abandon_lobby(app: App, input: Input) -> None:
            meta = _vk_metadata(input)
            data = _typed_data(input, LobbyActionInput)
            if meta is None or data is None:
                return
            await self._abandon_lobby(app, meta, data)

        @app.handler(background=False)
        async def move(app: App, input: Input) -> None:
            meta = _vk_metadata(input)
            data = _typed_data(input, MoveInput)
            if meta is None or data is None:
                return
            await self._move(app, meta, data)

        @app.handler(background=False)
        async def surrender(app: App, input: Input) -> None:
            meta = _vk_metadata(input)
            data = _typed_data(input, SurrenderInput)
            if meta is None or data is None:
                return
            await self._surrender(app, meta, data)

        @app.handler(background=False)
        async def back_to_menu(app: App, input: Input) -> None:
            meta = _vk_metadata(input)
            data = _typed_data(input, BackToMenuInput)
            if meta is None or data is None:
                return
            await self._back_to_menu(app, meta, data)

        @app.handler(background=False)
        async def close(app: App, input: Input) -> None:
            meta = _vk_metadata(input)
            data = _typed_data(input, CloseInput)
            if meta is None or data is None:
                return
            if data.owner_id == meta.user_id and meta.interface_id is not None:
                adapter = app.get_adapter(VKMessagesCallbackAdapter)
                await adapter.delete(meta.interface_id)

        @app.handler(background=False)
        async def noop(app: App, input: Input) -> None:
            return None

    async def _show_menu(
        self,
        app: App,
        peer_id: int,
        user_id: int,
        interface_id: VKMessageInterfaceId | None = None,
    ) -> None:
        draft = app.new_interface()
        create_payload = draft.bind(
            CreateLobbyInput(handler_name="ttt.create_lobby", owner_id=user_id)
        )
        join_payload = draft.bind(
            AskJoinCodeInput(handler_name="ttt.ask_join_code", owner_id=user_id)
        )
        close_payload = draft.bind(CloseInput(handler_name="ttt.close", owner_id=user_id))
        keyboard = Keyboard.from_rows(
            [
                [
                    Button.callback("Создать игру", payload=create_payload),
                    Button.callback("Подключиться к игре", payload=join_payload),
                    Button.callback("Закрыть", payload=close_payload),
                ]
            ]
        )
        if interface_id is None:
            await app.get_adapter(VKMessagesCallbackAdapter).send(
                peer_id,
                "Крестики-нолики. Выберите действие:",
                draft,
                keyboard=keyboard,
            )
        else:
            await app.get_adapter(VKMessagesCallbackAdapter).edit(
                interface_id,
                "Крестики-нолики. Выберите действие:",
                draft,
                keyboard=keyboard,
            )

    async def _create_lobby(
        self,
        app: App,
        metadata: VKMessageMetadata,
        event: CreateLobbyInput,
    ) -> None:
        interface_id = metadata.interface_id
        if event.owner_id != metadata.user_id or interface_id is None:
            return
        for _ in range(8):
            code = secrets.token_hex(3).upper()
            key = {
                "app": "ttt",
                "kind": "lobby",
                "group_id": metadata.group_id,
                "code": code,
            }
            async with app.lock(key):
                if await app.get_state(key) is not None:
                    continue
                owner = Participant(
                    user_id=metadata.user_id,
                    peer_id=metadata.peer_id,
                    interface_id=interface_id.model_dump(mode="json"),
                )
                await app.set_state(
                    key,
                    {"status": "waiting", "owner": owner.model_dump(mode="json")},
                )
                await self._render_lobby(app, interface_id, code)
                return
        raise RuntimeError("Не удалось подобрать свободный код игры")  # noqa: RUF001

    async def _render_lobby(
        self,
        app: App,
        interface_id: VKMessageInterfaceId,
        code: str,
    ) -> None:
        draft = app.new_interface()
        payload = draft.bind(LobbyActionInput(handler_name="ttt.abandon_lobby", code=code))
        keyboard = Keyboard.from_rows([[Button.callback("Покинуть подбор", payload=payload)]])
        await app.get_adapter(VKMessagesCallbackAdapter).edit(
            interface_id,
            f"Ожидаем второго игрока. Код игры: {code}",
            draft,
            keyboard=keyboard,
        )

    async def _ask_join_code(
        self,
        app: App,
        metadata: VKMessageMetadata,
        event: AskJoinCodeInput,
    ) -> None:
        if event.owner_id == metadata.user_id and metadata.interface_id is not None:
            await self._render_join_prompt(app, metadata.interface_id, metadata.user_id)

    async def _render_join_prompt(
        self,
        app: App,
        interface_id: VKMessageInterfaceId,
        owner_id: int,
    ) -> None:
        draft = app.new_interface()
        close_payload = draft.bind(
            CancelJoinInput(handler_name="ttt.cancel_join", owner_id=owner_id)
        )
        keyboard = Keyboard.from_rows([[Button.callback("Закрыть", payload=close_payload)]])
        await app.get_adapter(VKMessagesCallbackAdapter).edit(
            interface_id,
            "Введите код игры ответом на это сообщение:",
            draft,
            keyboard=keyboard,
        )

    async def _join_code(
        self,
        app: App,
        metadata: VKMessageMetadata,
        input: str,
    ) -> None:
        interface_id = metadata.interface_id
        if interface_id is None:
            return
        code = input.strip().upper()
        if len(code) != 6 or any(character not in "0123456789ABCDEF" for character in code):
            return
        lobby_key = {
            "app": "ttt",
            "kind": "lobby",
            "group_id": metadata.group_id,
            "code": code,
        }
        game_id = uuid4().hex
        game_key = {
            "app": "ttt",
            "kind": "game",
            "group_id": metadata.group_id,
            "game_id": game_id,
        }
        async with app.lock(lobby_key, game_key):
            lobby = await app.get_state(lobby_key)
            if lobby is None or lobby.get("status") != "waiting":
                return

            joiner = Participant(
                user_id=metadata.user_id,
                peer_id=metadata.peer_id,
                interface_id=interface_id.model_dump(mode="json"),
            )
            players = [lobby["owner"], joiner.model_dump(mode="json")]
            game = {
                "status": "active",
                "game_id": game_id,
                "players": players,
                "board": [None] * 9,
                "turn": secrets.randbelow(2),
                "winner": None,
            }
            await app.delete_state(lobby_key)
            await app.set_state(game_key, game)
            await self._render_game(app, metadata.group_id, game)

    async def _abandon_lobby(
        self,
        app: App,
        metadata: VKMessageMetadata,
        event: LobbyActionInput,
    ) -> None:
        key = {
            "app": "ttt",
            "kind": "lobby",
            "group_id": metadata.group_id,
            "code": event.code,
        }
        async with app.lock(key):
            lobby = await app.get_state(key)
            if lobby is None or lobby.get("status") != "waiting":
                return
            owner = lobby["owner"]
            if not _is_owner(owner, metadata):
                return
            await app.delete_state(key)
            if metadata.interface_id is not None:
                await app.get_adapter(VKMessagesCallbackAdapter).delete(metadata.interface_id)

    async def _move(self, app: App, metadata: VKMessageMetadata, event: MoveInput) -> None:
        key = {
            "app": "ttt",
            "kind": "game",
            "group_id": metadata.group_id,
            "game_id": event.game_id,
        }
        async with app.lock(key):
            game = await app.get_state(key)
            if game is None or not self._authorized_turn(game, metadata, event):
                return
            if not 0 <= event.cell < 9 or game["board"][event.cell] is not None:
                return
            game["board"][event.cell] = "X" if event.seat == 0 else "O"
            winner = _winner(game["board"])
            if winner is not None:
                game["status"] = "finished"
                game["winner"] = winner
                game["finish_reason"] = "line"
            elif all(game["board"]):
                game["status"] = "finished"
                game["winner"] = None
                game["finish_reason"] = "draw"
            else:
                game["turn"] = 1 - event.seat
            await app.set_state(key, game)
            await self._render_game(app, metadata.group_id, game)

    async def _surrender(
        self,
        app: App,
        metadata: VKMessageMetadata,
        event: SurrenderInput,
    ) -> None:
        key = {
            "app": "ttt",
            "kind": "game",
            "group_id": metadata.group_id,
            "game_id": event.game_id,
        }
        async with app.lock(key):
            game = await app.get_state(key)
            if (
                game is None
                or not self._authorized_player(game, metadata, event)
                or game["status"] != "active"
            ):
                return
            game["status"] = "finished"
            game["winner"] = 1 - event.seat
            game["finish_reason"] = "surrender"
            await app.set_state(key, game)
            await self._render_game(app, metadata.group_id, game)

    async def _back_to_menu(
        self,
        app: App,
        metadata: VKMessageMetadata,
        event: BackToMenuInput,
    ) -> None:
        key = {
            "app": "ttt",
            "kind": "game",
            "group_id": metadata.group_id,
            "game_id": event.game_id,
        }
        game = await app.get_state(key)
        if (
            game is None
            or game["status"] != "finished"
            or not self._participant_seat(game, metadata, event)
            or metadata.interface_id is None
        ):
            return
        await self._show_menu(app, metadata.peer_id, metadata.user_id, metadata.interface_id)

    def _authorized_turn(
        self,
        game: dict[str, Any] | None,
        metadata: VKMessageMetadata,
        event: MoveInput,
    ) -> bool:
        return (
            game is not None
            and game["status"] == "active"
            and game["turn"] == event.seat
            and self._participant_seat(game, metadata, event)
        )

    def _authorized_player(
        self,
        game: dict[str, Any] | None,
        metadata: VKMessageMetadata,
        event: GameActionInput,
    ) -> bool:
        return (
            game is not None
            and 0 <= event.seat < 2
            and self._participant_seat(game, metadata, event)
        )

    @staticmethod
    def _participant_seat(
        game: dict[str, Any],
        metadata: VKMessageMetadata,
        event: GameActionInput | MoveInput,
    ) -> bool:
        seat = getattr(event, "seat", None)
        if not isinstance(seat, int) or not 0 <= seat < 2:
            return False
        player = game["players"][seat]
        return (
            metadata.interface_id is not None
            and player["user_id"] == metadata.user_id
            and player["peer_id"] == metadata.peer_id
            and _stored_interface_id(player) == metadata.interface_id.model_dump(mode="json")
        )

    async def _render_game(self, app: App, group_id: int, game: dict[str, Any]) -> None:
        board_text = _board_text(game["board"])
        for seat, player in enumerate(game["players"]):
            interface_id = VKMessageInterfaceId.model_validate(_stored_interface_id(player))
            draft = app.new_interface()
            if game["status"] == "active":
                rows: list[list[KeyboardButton]] = []
                for row in range(3):
                    buttons = []
                    for column in range(3):
                        cell = row * 3 + column
                        value = game["board"][cell]
                        handler = (
                            "ttt.move" if seat == game["turn"] and value is None else "ttt.noop"
                        )
                        if handler == "ttt.move":
                            payload = draft.bind(
                                MoveInput(
                                    handler_name="ttt.move",
                                    game_id=game["game_id"],
                                    seat=seat,
                                    cell=cell,
                                )
                            )
                        else:
                            payload = draft.bind(NoopInput(handler_name="ttt.noop"))
                        label = value or str(cell + 1)
                        buttons.append(Button.callback(label, payload=payload))
                    rows.append(buttons)
                surrender = draft.bind(
                    SurrenderInput(
                        handler_name="ttt.surrender",
                        game_id=game["game_id"],
                        seat=seat,
                    )
                )
                rows.append([Button.callback("Покинуть игру", payload=surrender)])
                status = "Ваш ход" if game["turn"] == seat else "Ход соперника"
                text = f"{board_text}\n\n{status}. Вы играете за {'X' if seat == 0 else 'O'}."
            else:
                winner = game["winner"]
                if winner is None:
                    result = "Ничья."
                elif winner == seat:
                    result = "Вы победили."
                else:
                    result = "Вы проиграли."
                if game.get("finish_reason") == "surrender" and winner == seat:
                    result = "Соперник сдался. Вы победили."
                menu_payload = draft.bind(
                    BackToMenuInput(
                        handler_name="ttt.back_to_menu",
                        game_id=game["game_id"],
                        seat=seat,
                    )
                )
                close_payload = draft.bind(
                    CloseInput(
                        handler_name="ttt.close",
                        owner_id=player["user_id"],
                    )
                )
                rows = [
                    [
                        Button.callback("В меню", payload=menu_payload),  # noqa: RUF001
                        Button.callback("Закрыть", payload=close_payload),
                    ]
                ]
                text = f"{board_text}\n\nИгра завершена. {result}"  # noqa: RUF001
            keyboard = Keyboard.from_rows(rows)
            await app.get_adapter(VKMessagesCallbackAdapter).edit(
                interface_id,
                text,
                draft,
                keyboard=keyboard,
            )


async def make_callback_app(
    vk: VKClient,
    group_id: int,
    callback_url: str,
    *,
    storage_path: str = "tictactoe-state.json",
    secret: str | None = None,
):
    """Создаёт приложение; HTTP-сервер передаёт POST body в App.handle_input."""
    app = App(storage=JSONFileStorage(storage_path))
    callback_server: CallbackServer = await create_callback_server(
        vk,
        group_id,
        callback_url,
        title="vkuxt ttt",
        secret=secret,
    )
    app.add_adapter(
        VKMessagesCallbackAdapter,
        vk,
        group_id,
        callback_server=callback_server,
        dm_latest_interface=False,
    )
    TicTacToeBot(app)
    return app


def attach_fastapi(app: App):
    from fastapi import FastAPI, Request
    from fastapi.responses import PlainTextResponse

    web = FastAPI(lifespan=app.lifespan)

    @web.post("/vk/callback")
    async def callback(request: Request) -> PlainTextResponse:
        response = await app.handle_input(await request.body())
        return PlainTextResponse(response if isinstance(response, str) else "ok")

    return web


def _vk_metadata(event: Input) -> VKMessageMetadata | None:
    return event.metadata if isinstance(event.metadata, VKMessageMetadata) else None


def _typed_data[TicTacToeInputT: InputData](
    event: Input,
    model: type[TicTacToeInputT],
) -> TicTacToeInputT | None:
    if event.data is None:
        return None
    try:
        return model.model_validate(event.data.model_dump())
    except ValueError:
        return None


def _is_owner(owner: dict[str, Any], metadata: VKMessageMetadata) -> bool:
    return (
        owner["user_id"] == metadata.user_id
        and owner["peer_id"] == metadata.peer_id
        and metadata.interface_id is not None
        and _stored_interface_id(owner) == metadata.interface_id.model_dump(mode="json")
    )


def _stored_interface_id(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("interface_id", {})


def _winner(board: list[str | None]) -> int | None:
    lines = (
        (0, 1, 2),
        (3, 4, 5),
        (6, 7, 8),
        (0, 3, 6),
        (1, 4, 7),
        (2, 5, 8),
        (0, 4, 8),
        (2, 4, 6),
    )
    for a, b, c in lines:
        if board[a] is not None and board[a] == board[b] == board[c]:
            return 0 if board[a] == "X" else 1
    return None


def _board_text(board: list[str | None]) -> str:
    cells = [value or "·" for value in board]
    return "\n".join(" | ".join(cells[row : row + 3]) for row in range(0, 9, 3))
