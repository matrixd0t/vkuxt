"""Starlette-приложение для примера анкеты из ``examples/profile_form.py``.

Дополнительно включает подробное профилирование: каждая стадия обработки
callback-события пишется в journald с общим ``trace`` идентификатором.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

from profile_form import register_handlers  # pyright: ignore[reportAttributeAccessIssue]
from profiling import (
    ProfilingStorage,
    ProfilingVKMessagesCallbackAdapter,
    begin_trace,
    current_trace,
    install_profiling,
    install_vk_profiling,
    log_stage,
    reset_trace,
    summarize_callback,
)
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from vkx import VKClient
from vkx.server import CallbackServer

from vkuxt import App, JSONFileStorage

logger = logging.getLogger("profile-form")

GROUP_ID = int(os.environ.get("VK_GROUP_ID", "240432233"))
TOKEN = os.environ.get("VK_GROUP_TOKEN", "").strip()
CONFIRMATION_CODE = os.environ.get("VK_CONFIRMATION_CODE", "").strip()
SECRET = os.environ.get("VK_SECRET", "").strip() or None
STORAGE_PATH = os.environ.get(
    "STORAGE_PATH",
    "/opt/profile-form/data/state.json",
)


def _configure_logging() -> None:
    """Явный handler, иначе INFO-логи приложения теряются под uvicorn."""
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            )
        )
        logger.addHandler(handler)
    logger.propagate = False


_configure_logging()


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

    # Патч vkx ставим до probe, чтобы в лог попали и стартовые VK API-вызовы.
    install_vk_profiling()

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
    app = App(ProfilingStorage(JSONFileStorage(STORAGE_PATH)))
    app.add_adapter(
        ProfilingVKMessagesCallbackAdapter(
            app,
            vk,
            GROUP_ID,
            callback_server=callback_server,
            dm_latest_interface=True,
        )
    )
    install_profiling(app)
    register_handlers(app)
    await app.start()
    web.state.vkuxt = app
    web.state.vk = vk
    logger.info(
        "Приложение запущено для группы %s; профилирование включено",
        GROUP_ID,
    )
    try:
        yield
    finally:
        await app.close()
        await vk.aclose()
        logger.info("Приложение остановлено")


async def callback(request: Request) -> PlainTextResponse:
    app: App = request.app.state.vkuxt
    trace = current_trace()

    started = perf_counter()
    body = await request.body()
    body_took = perf_counter() - started

    summary = summarize_callback(body)
    if trace is not None:
        trace.apply_summary(summary)
    log_stage("http.body", bytes=len(body), **summary, took=body_took)

    started = perf_counter()
    try:
        result = await app.handle_input(body)
    except BaseException as exc:
        log_stage(
            "http.handle_input",
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            took=perf_counter() - started,
        )
        raise
    log_stage(
        "http.handle_input",
        status="ok",
        result="none" if result is None else "response",
        took=perf_counter() - started,
    )
    return PlainTextResponse("ok" if result is None else str(result))


async def health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def profile_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if request.url.path != "/vk/callback":
        return await call_next(request)

    trace, token = begin_trace()
    started = trace.started_at
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except BaseException as exc:
        log_stage(
            "http.error",
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            took=perf_counter() - started,
        )
        raise
    finally:
        log_stage(
            "http.total",
            status=status,
            took=perf_counter() - started,
        )
        reset_trace(token)


app = Starlette(
    routes=[
        Route("/vk/callback", callback, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ],
    middleware=[Middleware(BaseHTTPMiddleware, dispatch=profile_middleware)],
    lifespan=lifespan,
)
