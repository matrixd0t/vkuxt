from __future__ import annotations

import secrets
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel
from vkx import VKClient
from vkx.server import CallbackServer, create_callback_server

from vkuxt import (
    App,
    Context,
    InputData,
    InputModel,
    JSONFileStorage,
    KeyboardButtonColor,
    StateNamespace,
    VKInterface,
    VKMessageMetadata,
    VKMessagesCallbackAdapter,
)

_CODE_ALPHABET = frozenset("0123456789ABCDEF")
_WIN_LINES = (
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
)


def winner(board: Sequence[str | None]) -> int | None:
    for a, b, c in _WIN_LINES:
        if board[a] is not None and board[a] == board[b] == board[c]:
            return 0 if board[a] == "X" else 1
    return None


def board_text(board: Sequence[str | None]) -> str:
    cells = [value or "·" for value in board]
    return "\n".join(" | ".join(cells[row : row + 3]) for row in range(0, 9, 3))


def is_valid_code(code: str) -> bool:
    return len(code) == 6 and all(character in _CODE_ALPHABET for character in code)


if TYPE_CHECKING:
    from fastapi import FastAPI

LOBBY_TTL = 15 * 60
GAME_TTL = 60 * 60
CREATE_FAILED_VIEW = VKInterface(
    text="Не удалось создать игру. Попробуйте ещё раз.",
    ttl=LOBBY_TTL,
)


class TicTacToeState(StateNamespace):
    uid: int = 0
    code: str = ""
    game_id: str = ""
    seat: int = 0


type VKContext = Context[VKMessagesCallbackAdapter, TicTacToeState]


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


def lobby_key(metadata: VKMessageMetadata, code: str) -> dict[str, int | str]:
    return {
        "app": "ttt",
        "kind": "lobby",
        "group_id": metadata.group_id,
        "code": code,
    }


def game_key(metadata: VKMessageMetadata, game_id: str) -> dict[str, int | str]:
    return {
        "app": "ttt",
        "kind": "game",
        "group_id": metadata.group_id,
        "game_id": game_id,
    }


def player_at(game: GameState, seat: int) -> Participant | None:
    if 0 <= seat < len(game.players):
        return game.players[seat]
    return None


def is_player(
    player: Participant,
    metadata: VKMessageMetadata,
) -> bool:
    return (
        player.user_id == metadata.user_id
        and player.peer_id == metadata.peer_id
    )


def apply_screen_state(
    state: TicTacToeState,
    game: GameState,
    seat: int,
) -> None:
    player = game.players[seat]
    state.uid = player.user_id
    state.code = ""
    state.game_id = game.game_id
    state.seat = seat


def new_screen_state(game: GameState, seat: int) -> TicTacToeState:
    state = TicTacToeState()
    apply_screen_state(state, game, seat)
    return state


def apply_game_action(
    game: GameState,
    data: MoveInput | SurrenderInput,
    metadata: VKMessageMetadata,
) -> bool:
    if game.status != "active":
        return False
    player = player_at(game, data.seat)
    if player is None or not is_player(player, metadata):
        return False
    if isinstance(data, MoveInput):
        if game.turn != data.seat:
            return False
        if not 0 <= data.cell < 9 or game.board[data.cell] is not None:
            return False
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
        return True
    game.status = "finished"
    game.winner = 1 - data.seat
    game.finish_reason = "surrender"
    return True


def menu_view(uid: int) -> VKInterface:
    return VKInterface(
        text="Крестики-нолики. Выберите действие:",
        ttl=LOBBY_TTL,
        kb=[
            [
                VKInterface.callback(
                    "Создать игру",
                    CreateLobbyInput(uid=uid),
                    color=KeyboardButtonColor.POSITIVE,
                ),
                VKInterface.callback(
                    "Подключиться к игре",
                    AskJoinCodeInput(uid=uid),
                    color=KeyboardButtonColor.PRIMARY,
                ),
                VKInterface.callback(
                    "Закрыть",
                    CloseInput(uid=uid),
                    color=KeyboardButtonColor.NEGATIVE,
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
                    color=KeyboardButtonColor.NEGATIVE,
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
                    color=KeyboardButtonColor.NEGATIVE,
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
                            color=KeyboardButtonColor.PRIMARY,
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
                    color=KeyboardButtonColor.NEGATIVE,
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
                    color=KeyboardButtonColor.PRIMARY,
                ),
                VKInterface.callback(
                    "Закрыть",
                    CloseInput(uid=player.user_id),
                    color=KeyboardButtonColor.NEGATIVE,
                ),
            ]
        ]
        text = f"{rendered_board}\n\nИгра завершена. {result}"
    return VKInterface(text=text, kb=rows, ttl=GAME_TTL)


async def draw_game(
    ctx: VKContext,
    metadata: VKMessageMetadata,
    game: GameState,
) -> None:
    for seat, player in enumerate(game.players):
        view = game_view(game, seat)
        if player.peer_id == metadata.peer_id:
            apply_screen_state(ctx.state, game, seat)
            await ctx.redraw(view)
        else:
            handle = await ctx.adapter.redraw(view, peer_id=player.peer_id)
            if handle is not None:
                await ctx.app.set_state(
                    handle.state_key(),
                    new_screen_state(game, seat),
                    ttl=GAME_TTL,
                )


async def menu(
    ctx: VKContext,
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    if text.strip().casefold() not in {"начать", "меню"}:
        return
    ctx.state.uid = metadata.user_id
    await ctx.redraw(menu_view(metadata.user_id))


async def create_lobby(
    ctx: VKContext,
    metadata: VKMessageMetadata,
    data: CreateLobbyInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id:
        return
    for _ in range(8):
        code = secrets.token_hex(3).upper()
        key = lobby_key(metadata, code)
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
        ctx.state.uid = metadata.user_id
        ctx.state.code = code
        await ctx.redraw(lobby_view(code))
        return
    await ctx.redraw(CREATE_FAILED_VIEW)


async def ask_join_code(
    ctx: VKContext,
    metadata: VKMessageMetadata,
    data: AskJoinCodeInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id:
        return
    ctx.state.uid = metadata.user_id
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

    lobby_state_key = lobby_key(metadata, code)
    game_id = uuid4().hex
    game_state_key = game_key(metadata, game_id)
    # id игры новый, поэтому лочить game_state_key не нужно.
    async with ctx.app.lock(lobby_state_key):
        lobby = await ctx.app.get_state(lobby_state_key, LobbyState)
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
        await ctx.app.delete_state(lobby_state_key)
        await ctx.app.set_state(game_state_key, game, ttl=GAME_TTL)

    await draw_game(ctx, metadata, game)


async def cancel_join(
    ctx: VKContext,
    metadata: VKMessageMetadata,
    data: CancelJoinInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id:
        return
    if ctx.state.uid != metadata.user_id:
        return
    await ctx.redraw(None)


async def abandon_lobby(
    ctx: VKContext,
    metadata: VKMessageMetadata,
    data: LobbyActionInput,
    text: str,
) -> None:
    code = ctx.state.code
    if not code or code != data.code:
        return
    key = lobby_key(metadata, code)
    async with ctx.app.lock(key):
        lobby = await ctx.app.get_state(key, LobbyState)
        if lobby is None or lobby.status != "waiting":
            return
        if not is_player(lobby.owner, metadata):
            return
        await ctx.app.delete_state(key)
    await ctx.redraw(None)


async def game_action(
    ctx: VKContext,
    metadata: VKMessageMetadata,
    data: MoveInput | SurrenderInput,
    text: str,
) -> None:
    game_id = ctx.state.game_id
    seat = ctx.state.seat
    if game_id != data.game_id or seat != data.seat:
        return
    key = game_key(metadata, data.game_id)
    async with ctx.app.lock(key):
        game = await ctx.app.get_state(key, GameState)
        if game is None or not apply_game_action(game, data, metadata):
            return
        await ctx.app.set_state(key, game, ttl=GAME_TTL)

        # Рендер под тем же lock'ом сохраняет порядок досок.
        await draw_game(ctx, metadata, game)


async def back_to_menu(
    ctx: VKContext,
    metadata: VKMessageMetadata,
    data: BackToMenuInput,
    text: str,
) -> None:
    if ctx.state.game_id != data.game_id or ctx.state.seat != data.seat:
        return
    key = game_key(metadata, data.game_id)
    game = await ctx.app.get_state(key, GameState)
    if game is None or game.status != "finished":
        return
    player = player_at(game, data.seat)
    if player is None or not is_player(player, metadata):
        return
    ctx.state = TicTacToeState(uid=metadata.user_id)
    await ctx.redraw(menu_view(metadata.user_id))


async def close(
    ctx: VKContext,
    metadata: VKMessageMetadata,
    data: CloseInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id:
        return
    if ctx.state.uid != metadata.user_id:
        return
    await ctx.redraw(None)


async def noop(
    ctx: VKContext,
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
