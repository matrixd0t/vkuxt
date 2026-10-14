from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Self, TypeVar, cast, overload

from matrix_planner import Planner, every
from pydantic import BaseModel, ConfigDict, model_validator

from .codec import storage_key
from .interfaces import DEFAULT_TTL, Interface, InterfaceRecord, TTLValue, _DefaultTTL
from .locks import KeyedLockPool
from .storage import MemoryStorage, StateStorage

InputT = TypeVar("InputT", bound="InputModel")
StateT = TypeVar("StateT")
AdapterT = TypeVar("AdapterT", bound="Adapter")
Handler = Callable[..., Any]


class InputModel(BaseModel):
    """Валидированная модель входа; подклассы могут переопределить from_json."""

    @classmethod
    def from_json(cls: type[InputT], raw_json: str) -> InputT:
        return cls.model_validate_json(raw_json)


class StructuredInput(InputModel):
    """Типизированная смысловая часть события."""

    model_config = ConfigDict(validate_assignment=True)

    @model_validator(mode="after")
    def _ensure_json_serializable(self) -> Self:
        try:
            json.dumps(
                self.model_dump(mode="json", warnings="error"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except Exception as exc:
            raise ValueError("StructuredInput должен сериализоваться в строгий JSON") from exc
        return self


class InputData(StructuredInput):
    """База моделей типизированных данных, принимаемых хендлерами."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)


class InputMetadata(InputModel):
    """Метаданные события, специфичные для адаптера."""


@dataclass(frozen=True, slots=True, init=False)
class Input:
    metadata: InputMetadata
    data: InputData | None
    text: str | None
    interface_id: Any | None

    def __init__(
        self,
        metadata: InputMetadata,
        data: InputData | Mapping[str, Any] | None = None,
        text: str | None = None,
        interface_id: Any | None = None,
    ) -> None:
        if not isinstance(metadata, InputMetadata):
            raise TypeError("metadata должен быть InputMetadata")
        if isinstance(data, Mapping):
            data = InputData.model_validate(data)
        elif data is not None and not isinstance(data, InputData):
            raise TypeError("data должен быть InputData, словарём или None")
        if text is not None and not isinstance(text, str):
            raise TypeError("text должен быть строкой или None")
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "interface_id", interface_id)


class Adapter:
    def __init__(self, app: App) -> None:
        self.app = app

    async def parse_input(
        self,
        raw: str | bytes | Mapping[str, Any],
    ) -> Input | None:
        """Распознать сырой ввод или вернуть None, если формат неизвестен."""

    async def dispatch_input(self, event: Input) -> Any:
        return await self.app.dispatch_event(event)

    async def start(self) -> None:
        """Запустить приём событий или фоновый опрос адаптера."""

    async def close(self) -> None:
        """Остановить фоновые задачи адаптера."""


@dataclass(frozen=True, slots=True)
class _HandlerEntry:
    background: bool
    function: Handler


class App:
    def __init__(
        self,
        storage: StateStorage | None = None,
        *,
        ttl: float | None = None,
        planner: Planner | None = None,
        tokenize_long_input: bool = True,
    ) -> None:
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl должен быть больше нуля или None")
        self.storage = storage if storage is not None else MemoryStorage()
        self.ttl = ttl
        self.tokenize_long_input = tokenize_long_input
        self.planner = planner if planner is not None else Planner()
        self._owns_planner = planner is None
        self._handlers: list[_HandlerEntry] = []
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._locks = KeyedLockPool()
        self.services: dict[str, Any] = {}
        self._adapters: list[Adapter] = []
        self._started_adapters: list[Adapter] = []
        self._adapter_start_tasks: set[asyncio.Task[None]] = set()
        self._adapter_start_lock = asyncio.Lock()
        self._running = False
        self._closing = False
        self._cleanup_task = self.planner.task(
            every("1m"),
            name=f"vkuxt-storage-cleanup-{id(self)}",
        )(self.storage.cleanup_expired)

    @property
    def adapters(self) -> tuple[Adapter, ...]:
        return tuple(self._adapters)

    def get_adapter(self, adapter_type: type[AdapterT]) -> AdapterT:
        for adapter in self._adapters:
            if isinstance(adapter, adapter_type):
                return cast(AdapterT, adapter)
        raise LookupError(f"Адаптер {adapter_type.__name__} не зарегистрирован")

    def add_adapter(
        self,
        adapter_type: type[AdapterT],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> AdapterT:
        if self._closing:
            raise RuntimeError("Нельзя добавлять адаптер во время остановки приложения")
        loop = None
        if self._running:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError as exc:
                raise RuntimeError(
                    "Добавление адаптера во время работы требует активного event loop"
                ) from exc
        adapter = adapter_type(self, *args, **kwargs)
        self._adapters.append(adapter)
        if loop is not None:
            task = loop.create_task(self._start_adapter(adapter))
            self._adapter_start_tasks.add(task)
            task.add_done_callback(self._adapter_start_done)
        return adapter

    def add_handler(self, function: Handler, *, background: bool = True) -> Handler:
        self._handlers.append(_HandlerEntry(background, function))
        return function

    @overload
    def handler(self, function: Handler, *, background: bool = True) -> Handler: ...

    @overload
    def handler(self, function: None = None, *, background: bool = True) -> Callable[[Handler], Handler]: ...

    def handler(self, function: Handler | None = None, *, background: bool = True) -> Handler | Callable[[Handler], Handler]:
        if function is None:
            return lambda registered: self.add_handler(registered, background=background)
        return self.add_handler(function, background=background)

    async def dispatch_event(self, _input: Input) -> Any:
        entries = tuple(self._handlers)
        for entry in entries:
            if not entry.background:
                try:
                    result = await self._invoke(entry, _input)
                except StopIteration:
                    break
                except RuntimeError as exc:
                    # PEP 479 wraps StopIteration raised inside async handlers.
                    if isinstance(exc.__cause__, StopIteration):
                        break
                    raise
                if result is not None:
                    return result

        background_entries = tuple(entry for entry in entries if entry.background)
        if background_entries:
            task = asyncio.create_task(self._run_background_handlers(background_entries, _input))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_task_done)
        return None

    async def _run_background_handlers(
        self,
        entries: tuple[_HandlerEntry, ...],
        _input: Input,
    ) -> None:
        for entry in entries:
            try:
                await self._invoke(entry, _input)
            except StopIteration:
                return
            except RuntimeError as exc:
                if isinstance(exc.__cause__, StopIteration):
                    return
                raise

    def _background_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        if exception := task.exception():
            task.get_loop().call_exception_handler(
                {"message": "Background input handler failed", "exception": exception, "task": task}
            )

    def _adapter_start_done(self, task: asyncio.Task[None]) -> None:
        self._adapter_start_tasks.discard(task)
        if task.cancelled():
            return
        if exception := task.exception():
            task.get_loop().call_exception_handler(
                {"message": "Adapter startup failed", "exception": exception, "task": task}
            )

    async def _start_adapter(self, adapter: Adapter) -> None:
        async with self._adapter_start_lock:
            if adapter in self._started_adapters:
                return
            self._started_adapters.append(adapter)
            await adapter.start()

    async def _wait_for_adapter_starts(self) -> None:
        while self._adapter_start_tasks:
            tasks = tuple(self._adapter_start_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._adapter_start_tasks.difference_update(tasks)

    async def _invoke(self, entry: _HandlerEntry, _input: Input) -> Any:
        result = entry.function(self, _input)
        return await result if inspect.isawaitable(result) else result

    async def handle_input(self, raw: str | bytes | Mapping[str, Any]) -> Any:
        for adapter in self._adapters:
            event = await adapter.parse_input(raw)
            if event is not None:
                return await adapter.dispatch_input(event)
        return None

    def new_interface(self, *, ttl: TTLValue = DEFAULT_TTL) -> Interface:
        return Interface(ttl=ttl, tokenize_long_input=self.tokenize_long_input)

    async def register_interface(self, external_id: Any, interface: Interface) -> None:
        interface_key = storage_key("interface", external_id)
        old_record = (
            InterfaceRecord.from_json_data(json.loads(raw))
            if (raw := await self.storage.get(interface_key)) is not None
            else None
        )
        if isinstance(interface.ttl, _DefaultTTL):
            ttl = old_record.ttl if old_record is not None else self.ttl
        else:
            ttl = interface.ttl
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl должен быть больше нуля или None")
        record = InterfaceRecord(dict(interface.actions), ttl)
        await self.storage.set(interface_key, _json(record.to_json_data()), ttl=ttl)

    async def get_interface(self, external_id: Any) -> InterfaceRecord | None:
        raw = await self.storage.get(storage_key("interface", external_id))
        return InterfaceRecord.from_json_data(json.loads(raw)) if raw is not None else None

    async def remove_interface(self, external_id: Any) -> None:
        await self.storage.delete(storage_key("interface", external_id))

    async def get_state(self, key: Any, model: type[StateT] | None = None) -> Any | None:
        raw = await self.storage.get(storage_key("state", key))
        if raw is None:
            return None
        if model is None:
            return json.loads(raw)
        data = json.loads(raw)
        if isinstance(model, type) and issubclass(model, BaseModel):
            return model.model_validate(data)
        if hasattr(model, "from_json"):
            return model.from_json(raw)  # type: ignore[attr-defined, no-any-return]
        return model(**data)  # type: ignore[call-arg, no-any-return]

    async def set_state(self, key: Any, value: Any, *, ttl: TTLValue = DEFAULT_TTL) -> None:
        if isinstance(value, BaseModel) or hasattr(value, "model_dump"):
            data = value.model_dump(mode="json")
        elif hasattr(value, "__dataclass_fields__"):
            from dataclasses import asdict

            data = asdict(value)
        else:
            data = value
        lifetime = self.ttl if isinstance(ttl, _DefaultTTL) else ttl
        if lifetime is not None and lifetime <= 0:
            raise ValueError("ttl должен быть больше нуля или None")
        await self.storage.set(storage_key("state", key), _json(data), ttl=lifetime)

    async def delete_state(self, key: Any) -> None:
        await self.storage.delete(storage_key("state", key))

    def lock(self, *keys: Any):
        return self._locks.lock(*keys)

    async def start(self) -> None:
        if self._closing:
            raise RuntimeError("Нельзя запустить приложение во время остановки")
        if self._running:
            return
        adapters = tuple(self._adapters)
        self._running = True
        try:
            if self._owns_planner:
                await self.planner.start()
            for adapter in adapters:
                await self._start_adapter(adapter)
        except (Exception, asyncio.CancelledError) as startup_error:
            try:
                await self._wait_for_adapter_starts()
                await self._stop_adapters()
            except (Exception, asyncio.CancelledError) as close_error:  # noqa: BLE001
                startup_error.add_note(
                    f"Ошибка остановки адаптера после сбоя запуска: {close_error!r}"
                )
            finally:
                try:
                    if self._owns_planner:
                        await self.planner.stop()
                    else:
                        await self.planner.remove(self._cleanup_task.task_id)
                except (Exception, asyncio.CancelledError) as planner_error:  # noqa: BLE001
                    startup_error.add_note(
                        f"Ошибка остановки planner после сбоя запуска: {planner_error!r}"
                    )
                self._running = False
            raise

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._running = False
        try:
            try:
                await self._wait_for_adapter_starts()
                await self._stop_adapters()
            finally:
                try:
                    while self._background_tasks:
                        await asyncio.gather(
                            *tuple(self._background_tasks), return_exceptions=True
                        )
                finally:
                    if self._owns_planner:
                        await self.planner.stop()
                    else:
                        await self.planner.remove(self._cleanup_task.task_id)
        finally:
            self._running = False
            self._closing = False

    async def _stop_adapters(self) -> None:
        shutdown_error: BaseException | None = None
        while self._started_adapters:
            adapter = self._started_adapters.pop()
            try:
                await adapter.close()
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
                if shutdown_error is None:
                    shutdown_error = exc
                else:
                    shutdown_error.add_note(f"Ошибка остановки другого адаптера: {exc!r}")
        if shutdown_error is not None:
            raise shutdown_error

    @asynccontextmanager
    async def lifespan(self, _asgi_app: Any = None) -> AsyncIterator[None]:
        await self.start()
        try:
            yield
        finally:
            await self.close()

    async def __aenter__(self) -> App:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Данные не сериализуются в JSON: {exc}") from exc
