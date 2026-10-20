"""Starlette-приложение для примера анкеты из ``examples/profile_form.py``."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from profile_form import register_handlers
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from vkx import VKClient
from vkx.server import CallbackServer

from vkuxt import App, JSONFileStorage, VKMessagesCallbackAdapter

logger = logging.getLogger("profile-form")

GROUP_ID = int(os.environ.get("VK_GROUP_ID", "240432233"))
TOKEN = os.environ.get("VK_GROUP_TOKEN", "").strip()
CONFIRMATION_CODE = os.environ.get("VK_CONFIRMATION_CODE", "").strip()
SECRET = os.environ.get("VK_SECRET", "").strip() or None
STORAGE_PATH = os.environ.get(
    "STORAGE_PATH",
    "/opt/profile-form/data/state.json",
)


@asynccontextmanager
async def lifespan(web: Starlette) -> AsyncIterator[None]:
    if not TOKEN:
        raise RuntimeError("VK_GROUP_TOKEN не задан в /opt/profile-form/.env")
    if not CONFIRMATION_CODE:
        raise RuntimeError(
            "VK_CONFIRMATION_CODE не задан в /opt/profile-form/.env; "
            "скопируйте строку подтверждения из настроек Callback API"
        )

    Path(STORAGE_PATH).parent.mkdir(parents=True, exist_ok=True)

    vk = VKClient(tokens=[TOKEN])
    await vk.probe()
    if vk.group_id != GROUP_ID:
        raise RuntimeError(
            f"Токен принадлежит группе {vk.group_id}, а ожидается {GROUP_ID}"
        )

    callback_server = CallbackServer(
        GROUP_ID,
        secret=SECRET,
        confirmation_code=CONFIRMATION_CODE,
    )
    app = App(JSONFileStorage(STORAGE_PATH))
    app.add_adapter(
        VKMessagesCallbackAdapter(
            app,
            vk,
            GROUP_ID,
            callback_server=callback_server,
            dm_latest_interface=True,
        )
    )
    register_handlers(app)
    await app.start()
    web.state.vkuxt = app
    web.state.vk = vk
    logger.info("Приложение запущено для группы %s", GROUP_ID)
    try:
        yield
    finally:
        await app.close()
        await vk.aclose()
        logger.info("Приложение остановлено")


async def callback(request: Request) -> PlainTextResponse:
    app: App = request.app.state.vkuxt
    response = await app.handle_input(await request.body())
    return PlainTextResponse("ok" if response is None else str(response))


async def health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


app = Starlette(
    routes=[
        Route("/vk/callback", callback, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ],
    lifespan=lifespan,
)
