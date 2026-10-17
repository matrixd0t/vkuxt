from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel
from vkx import VKClient
from vkx.models import KeyboardButtonColor as col
from vkx.server import CallbackServer, create_callback_server

from vkuxt import (
    App,
    Context,
    InputData,
    InputModel,
    JSONFileStorage,
    VKInterface,
    VKMessageMetadata,
    VKMessagesCallbackAdapter,
)

from .tictactoe_rules import board_text, is_valid_code, winner

if TYPE_CHECKING:
    from fastapi import FastAPI

LOBBY_TTL = 15 * 60
GAME_TTL = 60 * 60
CREATE_FAILED_VIEW = VKInterface(
    text="Не удалось создать игру. Попробуйте ещё раз.",
    ttl=LOBBY_TTL,
)
type VKContext = Context[VKMessagesCallbackAdapter]


class TicTacToeInput(InputData):
    uid: int | None = None


class CreateLobbyInput(TicTacToeInput):
    handler_name: Literal["ttt.create_lobby"] = "ttt.create_lobby"


class AskJoinCodeInput(TicTacToeInput):
    handler_name: Literal["ttt.ask_join_code"] = "ttt.ask_join_code"


class CancelJoinInput(TicTacToeInput):
    handler_name: Literal["ttt.cancel_join"] = "ttt.cancel_join"


class CloseInput(TicTacToeInput):
    handler_name: Literal["ttt.close"] = "ttt.close"


class NoopInput(TicTacToeInput):
    handler_name: Literal["ttt.noop"] = "ttt.noop"


class MoveInput(TicTacToeInput):
    handler_name: Literal["ttt.move"] = "ttt.move"
    game_id: str
    seat: int
    cell: int


class GameActionInput(TicTacToeInput):
    game_id: str
    seat: int


class SurrenderInput(GameActionInput):
    handler_name: Literal["ttt.surrender"] = "ttt.surrender"


class BackToMenuInput(GameActionInput):
    handler_name: Literal["ttt.back_to_menu"] = "ttt.back_to_menu"


class LobbyActionInput(TicTacToeInput):
    handler_name: Literal["ttt.abandon_lobby"] = "ttt.abandon_lobby"
    code: str


class Participant(InputModel):
    user_id: int
    peer_id: int


class LobbyState(BaseModel):
    status: Literal["waiting"] = "waiting"
    owner: Participant


class GameState(BaseModel):
    status: Literal["active", "finished"]
    game_id: str
    players: list[Participant]
    board: list[str | None]
    turn: int
    winner: int | None = None
    finish_reason: Literal["line", "draw", "surrender"] | None = None


def menu_view(uid: int) -> VKInterface:
    return VKInterface(
        text="Крестики-нолики. Выберите действие:",
        ttl=LOBBY_TTL,
        kb=[
            [
                VKInterface.callback(
                    "Создать игру",
                    CreateLobbyInput(uid=uid),
                    color=col.POSITIVE,
                ),
                VKInterface.callback(
                    "Подключиться к игре",
                    AskJoinCodeInput(uid=uid),
                    color=col.PRIMARY,
                ),
                VKInterface.callback(
                    "Закрыть",
                    CloseInput(uid=uid),
                    color=col.NEGATIVE,
                ),
            ]
        ],
    )


def lobby_view(code: str) -> VKInterface:
    return VKInterface(
        text=f"Ожидаем второго игрока. Код игры: {code}",
        ttl=LOBBY_TTL,
        kb=[
            [
                VKInterface.callback(
                    "Покинуть подбор",
                    LobbyActionInput(code=code),
                    color=col.NEGATIVE,
                )
            ]
        ],
    )


def join_prompt_view(uid: int) -> VKInterface:
    return VKInterface(
        text="Введите код игры ответом на это сообщение:",
        ttl=LOBBY_TTL,
        kb=[
            [
                VKInterface.callback(
                    "Закрыть",
                    CancelJoinInput(uid=uid),
                    color=col.NEGATIVE,
                )
            ]
        ],
    )


def game_view(game: GameState, seat: int) -> VKInterface:
    player = game.players[seat]
    rendered_board = board_text(game.board)
    if game.status == "active":
        rows = []
        for row in range(3):
            buttons = []
            for column in range(3):
                cell = row * 3 + column
                value = game.board[cell]
                if seat == game.turn and value is None:
                    buttons.append(
                        VKInterface.callback(
                            str(cell + 1),
                            MoveInput(
                                game_id=game.game_id,
                                seat=seat,
                                cell=cell,
                            ),
                            color=col.PRIMARY,
                        )
                    )
                else:
                    buttons.append(
                        VKInterface.callback(value or str(cell + 1), NoopInput())
                    )
            rows.append(buttons)
        rows.append(
            [
                VKInterface.callback(
                    "Покинуть игру",
                    SurrenderInput(game_id=game.game_id, seat=seat),
                    color=col.NEGATIVE,
                )
            ]
        )
        status = "Ваш ход" if game.turn == seat else "Ход соперника"
        text = (
            f"{rendered_board}\n\n"
            f"{status}. Вы играете за {'X' if seat == 0 else 'O'}."
        )
    else:
        winner_seat = game.winner
        if winner_seat is None:
            result = "Ничья."
        elif winner_seat == seat:
            result = "Вы победили."
        else:
            result = "Вы проиграли."
        if game.finish_reason == "surrender" and winner_seat == seat:
            result = "Соперник сдался. Вы победили."
        rows = [
            [
                VKInterface.callback(
                    "В меню",
                    BackToMenuInput(game_id=game.game_id, seat=seat),
                    color=col.PRIMARY,
                ),
                VKInterface.callback(
                    "Закрыть",
                    CloseInput(uid=player.user_id),
                    color=col.NEGATIVE,
                ),
            ]
        ]
        text = f"{rendered_board}\n\nИгра завершена. {result}"
    return VKInterface(text=text, kb=rows, ttl=GAME_TTL)


async def menu(
    ctx: Context,
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    if text.strip().casefold() not in {"начать", "меню"}:
        return
    ctx.state["uid"] = metadata.user_id
    await ctx.redraw(menu_view(metadata.user_id))


async def create_lobby(
    ctx: Context,
    metadata: VKMessageMetadata,
    data: CreateLobbyInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id:
        return
    for _ in range(8):
        code = secrets.token_hex(3).upper()
        key = {
            "app": "ttt",
            "kind": "lobby",
            "group_id": metadata.group_id,
            "code": code,
        }
        async with ctx.app.lock(key):
            if await ctx.app.get_state(key, LobbyState) is not None:
                continue
            owner = Participant(
                user_id=metadata.user_id,
                peer_id=metadata.peer_id,
            )
            await ctx.app.set_state(
                key,
                LobbyState(owner=owner),
                ttl=LOBBY_TTL,
            )
        ctx.state["uid"] = metadata.user_id
        ctx.state["code"] = code
        await ctx.redraw(lobby_view(code))
        return
    await ctx.redraw(CREATE_FAILED_VIEW)


async def ask_join_code(
    ctx: Context,
    metadata: VKMessageMetadata,
    data: AskJoinCodeInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id:
        return
    ctx.state["uid"] = metadata.user_id
    await ctx.redraw(join_prompt_view(metadata.user_id))


async def join_code(
    ctx: VKContext,
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    if metadata.event_type != "message_new":
        return
    code = text.strip().upper()
    if not is_valid_code(code):
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
    async with ctx.app.lock(lobby_key, game_key):
        lobby = await ctx.app.get_state(lobby_key, LobbyState)
        if lobby is None or lobby.status != "waiting":
            return
        if lobby.owner.user_id == metadata.user_id:
            return
        joiner = Participant(
            user_id=metadata.user_id,
            peer_id=metadata.peer_id,
        )
        game = GameState(
            status="active",
            game_id=game_id,
            players=[lobby.owner, joiner],
            board=[None] * 9,
            turn=secrets.randbelow(2),
        )
        await ctx.app.delete_state(lobby_key)
        await ctx.app.set_state(game_key, game, ttl=GAME_TTL)

    for seat, player in enumerate(game.players):
        screen_state = {
            "game_id": game.game_id,
            "seat": seat,
            "uid": player.user_id,
        }
        view = game_view(game, seat)
        if player.peer_id == metadata.peer_id:
            ctx.state.update(screen_state)
            await ctx.redraw(view)
        else:
            handle = await ctx.adapter.redraw(view, peer_id=player.peer_id)
            if handle is not None:
                await ctx.app.set_state(handle.state_key(), screen_state, ttl=GAME_TTL)


async def cancel_join(
    ctx: Context,
    metadata: VKMessageMetadata,
    data: CancelJoinInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id:
        return
    if ctx.state.get("uid") != metadata.user_id:
        return
    await ctx.redraw(None)


async def abandon_lobby(
    ctx: Context,
    metadata: VKMessageMetadata,
    data: LobbyActionInput,
    text: str,
) -> None:
    code = ctx.state.get("code")
    if not isinstance(code, str) or code != data.code:
        return
    key = {
        "app": "ttt",
        "kind": "lobby",
        "group_id": metadata.group_id,
        "code": code,
    }
    async with ctx.app.lock(key):
        lobby = await ctx.app.get_state(key, LobbyState)
        if lobby is None or lobby.status != "waiting":
            return
        owner = lobby.owner
        if (
            owner.user_id != metadata.user_id
            or owner.peer_id != metadata.peer_id
        ):
            return
        await ctx.app.delete_state(key)
    await ctx.redraw(None)


async def game_action(
    ctx: VKContext,
    metadata: VKMessageMetadata,
    data: MoveInput | SurrenderInput,
    text: str,
) -> None:
    game_id = ctx.state.get("game_id")
    seat = ctx.state.get("seat")
    if game_id != data.game_id or seat != data.seat:
        return
    key = {
        "app": "ttt",
        "kind": "game",
        "group_id": metadata.group_id,
        "game_id": data.game_id,
    }
    async with ctx.app.lock(key):
        game = await ctx.app.get_state(key, GameState)
        if game is None or game.status != "active":
            return
        if not 0 <= data.seat < len(game.players):
            return
        player = game.players[data.seat]
        if (
            player.user_id != metadata.user_id
            or player.peer_id != metadata.peer_id
        ):
            return

        if isinstance(data, MoveInput):
            if game.turn != data.seat:
                return
            if not 0 <= data.cell < 9 or game.board[data.cell] is not None:
                return
            game.board[data.cell] = "X" if data.seat == 0 else "O"
            won_seat = winner(game.board)
            if won_seat is not None:
                game.status = "finished"
                game.winner = won_seat
                game.finish_reason = "line"
            elif all(game.board):
                game.status = "finished"
                game.winner = None
                game.finish_reason = "draw"
            else:
                game.turn = 1 - data.seat
        else:
            game.status = "finished"
            game.winner = 1 - data.seat
            game.finish_reason = "surrender"
        await ctx.app.set_state(key, game, ttl=GAME_TTL)

        # Рендер под тем же lock'ом сохраняет порядок досок.
        for render_seat, render_player in enumerate(game.players):
            screen_state = {
                "game_id": game.game_id,
                "seat": render_seat,
                "uid": render_player.user_id,
            }
            view = game_view(game, render_seat)
            if render_player.peer_id == metadata.peer_id:
                ctx.state.update(screen_state)
                await ctx.redraw(view)
            else:
                handle = await ctx.adapter.redraw(
                    view,
                    peer_id=render_player.peer_id,
                )
                if handle is not None:
                    await ctx.app.set_state(
                        handle.state_key(),
                        screen_state,
                        ttl=GAME_TTL,
                    )


async def back_to_menu(
    ctx: Context,
    metadata: VKMessageMetadata,
    data: BackToMenuInput,
    text: str,
) -> None:
    if ctx.state.get("game_id") != data.game_id or ctx.state.get("seat") != data.seat:
        return
    key = {
        "app": "ttt",
        "kind": "game",
        "group_id": metadata.group_id,
        "game_id": data.game_id,
    }
    game = await ctx.app.get_state(key, GameState)
    if game is None or game.status != "finished":
        return
    if not 0 <= data.seat < len(game.players):
        return
    player = game.players[data.seat]
    if (
        player.user_id != metadata.user_id
        or player.peer_id != metadata.peer_id
    ):
        return
    ctx.state.clear()
    ctx.state["uid"] = metadata.user_id
    await ctx.redraw(menu_view(metadata.user_id))


async def close(
    ctx: Context,
    metadata: VKMessageMetadata,
    data: CloseInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id:
        return
    if ctx.state.get("uid") != metadata.user_id:
        return
    await ctx.redraw(None)


async def noop(
    ctx: Context,
    metadata: VKMessageMetadata,
    data: NoopInput,
    text: str,
) -> None:
    return None


def register_handlers(app: App) -> None:
    for handler in (
        menu,
        create_lobby,
        ask_join_code,
        join_code,
        cancel_join,
        abandon_lobby,
        game_action,
        back_to_menu,
        close,
        noop,
    ):
        app.add_handler(handler)


async def make_callback_app(
    vk: VKClient,
    group_id: int,
    callback_url: str,
    *,
    storage_path: str = "tictactoe-state.json",
    secret: str | None = None,
) -> App:
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
        VKMessagesCallbackAdapter(
            app,
            vk,
            group_id,
            callback_server=callback_server,
            dm_latest_interface=False,
        ),
    )
    register_handlers(app)
    return app


def attach_fastapi(app: App) -> FastAPI:
    from fastapi import FastAPI, Request
    from fastapi.responses import PlainTextResponse

    web = FastAPI(lifespan=app.lifespan)

    @web.post("/vk/callback")
    async def callback(request: Request) -> PlainTextResponse:
        response = await app.handle_input(await request.body())
        return PlainTextResponse("ok" if response is None else str(response))

    return web
