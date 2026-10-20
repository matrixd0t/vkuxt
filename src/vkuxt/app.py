from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import types
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import asdict, dataclass, is_dataclass
from typing import (
    Annotated,
    Any,
    ClassVar,
    ForwardRef,
    Literal,
    NoDefault,
    TypeAliasType,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

from matrix_planner import Planner, every
from pydantic import BaseModel, ValidationError

from .codec import KeyLike, encode_key, storage_key
from .interfaces import (
    DEFAULT_TTL,
    Interface,
    InterfaceHandle,
    InterfaceRecord,
    TTLValue,
    _DefaultTTL,
)
from .locks import KeyedLockPool, LockedKeys
from .models import BaseMetadata as BaseMetadata
from .models import InputData as InputData
from .models import InputModel as InputModel
from .models import StateNamespace as StateNamespace
from .storage import MemoryStorage, StateStorage

logger = logging.getLogger("vkuxt")

_MISSING = object()
StateT = TypeVar("StateT")
AdapterT = TypeVar("AdapterT", bound="Adapter")
Handler = Callable[..., Any]


@dataclass(frozen=True, slots=True, init=False)
class Input[DataT, MetadataT: BaseMetadata]:
    data: DataT
    metadata: MetadataT
    text: str | None
    interface: InterfaceHandle[Any] | None

    def __init__(
        self,
        metadata: MetadataT,
        data: InputData | Mapping[str, Any] | None = None,
        text: str | None = None,
        interface: InterfaceHandle[Any] | None = None,
    ) -> None:
        if not isinstance(metadata, BaseMetadata):
            raise TypeError("metadata должен быть BaseMetadata")
        if isinstance(data, Mapping):
            data = InputData.model_validate(data)
        elif data is not None and not isinstance(data, InputData):
            raise TypeError("data должен быть InputData, словарём или None")
        if text is not None and not isinstance(text, str):
            raise TypeError("text должен быть строкой или None")
        if interface is not None and not isinstance(interface, InterfaceHandle):
            raise TypeError("interface должен быть InterfaceHandle или None")
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "interface", interface)


class Adapter:
    interface_type: ClassVar[type[Interface[Any]]] = Interface
    platform: ClassVar[str] = ""
    """Имя платформы; входит в ключи интерфейсов и связанного состояния."""

    def __init__(self, app: App) -> None:
        self.app = app

    def context_key(self, metadata: BaseMetadata) -> KeyLike:
        """Ключ состояния контекста, не привязанного к интерфейсу."""
        return {
            "adapter": self.platform or f"{type(self).__module__}.{type(self).__qualname__}",
            "metadata": encode_key(metadata),
        }

    def new_interface(
        self,
        *,
        text: str = "",
        rows: Sequence[Sequence[Any]] = (),
        ttl: TTLValue = DEFAULT_TTL,
    ) -> Interface[Any]:
        """Создать представление интерфейса, подходящее этому адаптеру."""
        return self.interface_type(text=text, kb=rows, ttl=ttl)

    async def parse_input(
        self,
        raw: str | bytes | Mapping[str, Any],
    ) -> Input | None:
        """Распознать сырой ввод или вернуть None, если формат неизвестен."""

    async def dispatch_input(self, event: Input) -> Any:
        return await self.app.dispatch_event(event, self)

    async def redraw(
        self,
        interface: Interface[Any] | None,
        *,
        target: InterfaceHandle[Any] | None = None,
    ) -> InterfaceHandle[Any] | None:
        """Перерисовать интерфейс; транспортная реализация обязательна."""
        raise NotImplementedError("Адаптер не поддерживает redraw()")

    async def start(self) -> None:
        """Запустить приём событий или фоновый опрос адаптера."""

    async def close(self) -> None:
        """Остановить фоновые задачи адаптера."""


@dataclass(slots=True, init=False)
class Context[
    AdapterT: Adapter = Adapter,
    StateT: StateNamespace | None = StateNamespace,
]:
    """Контекст одного хендлера: интерфейс, состояние и доступ к приложению.

    ``state`` — изменяемый JSON-namespace, привязанный к текущему интерфейсу.
    Поля можно читать и писать как атрибуты; необъявленные атрибуты
    сохраняются. После успешного завершения хендлера состояние сохраняется
    автоматически. Второй параметр ``Context`` задаёт модель (или ``Union``
    моделей) состояния, при которой вызывается хендлер.
    """

    app: App
    adapter: AdapterT
    interface: InterfaceHandle[Any] | None
    metadata: BaseMetadata
    _state: StateNamespace
    _state_key: KeyLike | None
    _state_ttl: TTLValue
    _state_cache: dict[str, dict[str, Any] | None] | None

    def __init__(
        self,
        app: App,
        adapter: AdapterT,
        interface: InterfaceHandle[Any] | None,
        metadata: BaseMetadata,
        state: StateNamespace,
        _state_key: KeyLike | None,
        _state_ttl: TTLValue = DEFAULT_TTL,
        _state_cache: dict[str, dict[str, Any] | None] | None = None,
    ) -> None:
        self.app = app
        self.adapter = adapter
        self.interface = interface
        self.metadata = metadata
        self.state = state
        self._state_key = _state_key
        self._state_ttl = _state_ttl
        self._state_cache = _state_cache

    @property
    def state(self) -> StateT:
        return cast(StateT, self._state)

    @state.setter
    def state(self, value: StateNamespace) -> None:
        if not isinstance(value, StateNamespace):
            raise TypeError("state должен быть экземпляром StateNamespace")
        self._state = value

    async def redraw(
        self,
        interface: Interface[Any] | None,
        *,
        state: StateNamespace | None = None,
    ) -> InterfaceHandle[Any] | None:
        """Перерисовать текущий интерфейс и перенести состояние на новый."""
        if interface is None and state is not None:
            raise ValueError("Нельзя передать state при удалении интерфейса")
        if state is not None:
            self.state = state
        previous = self.interface
        previous_key = self._state_key
        result = await self.adapter.redraw(interface, target=previous)
        self.interface = result
        if result is None:
            if previous_key is not None:
                await self.app.delete_state(previous_key)
                self._forget_cached_state(previous_key)
                self._state_key = None
                self._state_ttl = DEFAULT_TTL
            return None

        new_key = result.state_key()
        if new_key != previous_key:
            await self.app.set_state(new_key, self._state, ttl=result.ttl)
            self._remember_cached_state(new_key)
            if previous_key is not None and (
                previous is None
                or await self.app._get_interface_record(
                    previous.id,
                    platform=previous.platform,
                )
                is None
            ):
                await self.app.delete_state(previous_key)
                self._forget_cached_state(previous_key)
            self._state_key = new_key
        elif state is not None:
            await self.app.set_state(new_key, self._state, ttl=result.ttl)
            self._remember_cached_state(new_key)
        self._state_ttl = result.ttl
        return result

    @overload
    def locked_state(
        self,
        key: KeyLike,
        model: None = None,
        *,
        ttl: TTLValue = DEFAULT_TTL,
    ) -> AbstractAsyncContextManager[Any | None]: ...

    @overload
    def locked_state[ModelT](
        self,
        key: KeyLike,
        model: type[ModelT],
        *,
        ttl: TTLValue = DEFAULT_TTL,
    ) -> AbstractAsyncContextManager[ModelT | None]: ...

    @overload
    def locked_state[ModelT](
        self,
        key: KeyLike,
        model: type[ModelT],
        *,
        default: ModelT | Callable[[], ModelT],
        ttl: TTLValue = DEFAULT_TTL,
    ) -> AbstractAsyncContextManager[ModelT]: ...

    def locked_state(
        self,
        key: KeyLike,
        model: type[Any] | None = None,
        *,
        default: Any = _MISSING,
        ttl: TTLValue = DEFAULT_TTL,
    ) -> AbstractAsyncContextManager[Any]:
        """Заблокировать ключ, загрузить состояние и сохранить изменения при выходе."""
        if default is _MISSING:
            return self.app.locked_state(key, model, ttl=ttl)
        return self.app.locked_state(key, model, default=default, ttl=ttl)  # type: ignore[arg-type]

    async def _save(self) -> None:
        if self._state_key is None:
            return
        if self.interface is not None and self.interface.deleted:
            self._forget_cached_state(self._state_key)
            return
        await self.app.set_state(self._state_key, self._state, ttl=self._state_ttl)
        self._remember_cached_state(self._state_key)

    def _remember_cached_state(self, state_key: KeyLike) -> None:
        if self._state_cache is not None:
            state = (
                self._state.model_dump(mode="json")
                if isinstance(self._state, BaseModel)
                else self._state
            )
            self._state_cache[encode_key(state_key)] = state

    def _forget_cached_state(self, state_key: KeyLike) -> None:
        if self._state_cache is not None:
            self._state_cache.pop(encode_key(state_key), None)


@dataclass(frozen=True, slots=True)
class _HandlerEntry:
    order: int
    background: bool
    function: Handler
    metadata_types: tuple[type[BaseMetadata], ...]
    data_models: tuple[type[InputData], ...]
    accepts_missing_data: bool
    state_models: tuple[type[StateNamespace], ...]
    accepts_empty_state: bool


class StopHandlerChain(Exception):
    """Остановить вызов следующих хендлеров в текущей фазе."""


class App:
    def __init__(
        self,
        storage: StateStorage | None = None,
        *,
        ttl: float | None = None,
        planner: Planner | None = None,
        tokenize_long_input: bool = True,
        close_timeout: float | None = 10.0,
        adapter_close_timeout: float | None = 10.0,
    ) -> None:
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl должен быть больше нуля или None")
        if close_timeout is not None and close_timeout <= 0:
            raise ValueError("close_timeout должен быть больше нуля или None")
        if adapter_close_timeout is not None and adapter_close_timeout <= 0:
            raise ValueError("adapter_close_timeout должен быть больше нуля или None")
        self.storage = storage if storage is not None else MemoryStorage()
        self.ttl = ttl
        self.tokenize_long_input = tokenize_long_input
        self.close_timeout = close_timeout
        self.adapter_close_timeout = adapter_close_timeout
        self.planner = planner if planner is not None else Planner()
        self._owns_planner = planner is None
        self._handlers: list[_HandlerEntry] = []
        self._handlers_by_metadata: dict[type[BaseMetadata], list[_HandlerEntry]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._locks = KeyedLockPool()
        # Свободный контейнер для сервисов приложения; vkuxt его не использует.
        self.services: dict[str, Any] = {}
        self._adapters: list[Adapter] = []
        self._started_adapters: list[Adapter] = []
        self._adapter_start_tasks: set[asyncio.Task[None]] = set()
        self._adapter_start_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._running = False
        self._closing = False
        self._closed = False
        self._close_owner: asyncio.Task[Any] | None = None
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
        adapter: AdapterT,
        /,
    ) -> AdapterT:
        if not isinstance(adapter, Adapter):
            raise TypeError("add_adapter ожидает экземпляр Adapter")
        if adapter.app is not self:
            raise ValueError("Адаптер принадлежит другому App")
        if any(existing is adapter for existing in self._adapters):
            raise ValueError("Адаптер уже добавлен")
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
        self._adapters.append(adapter)
        if loop is not None:
            task = loop.create_task(self._start_adapter(adapter))
            self._adapter_start_tasks.add(task)
            task.add_done_callback(self._adapter_start_done)
        return adapter

    def add_handler(self, function: Handler, *, background: bool = True) -> Handler:
        localns = _caller_localns(function)
        (
            metadata_types,
            data_models,
            accepts_missing_data,
            state_models,
            accepts_empty_state,
        ) = _handler_annotations(function, localns)
        entry = _HandlerEntry(
            order=len(self._handlers),
            background=background,
            function=function,
            metadata_types=metadata_types,
            data_models=data_models,
            accepts_missing_data=accepts_missing_data,
            state_models=state_models,
            accepts_empty_state=accepts_empty_state,
        )
        self._handlers.append(entry)
        for metadata_type in metadata_types:
            self._handlers_by_metadata.setdefault(metadata_type, []).append(entry)
        return function

    @overload
    def handler(self, function: Handler, *, background: bool = True) -> Handler: ...

    @overload
    def handler(self, function: None = None, *, background: bool = True) -> Callable[[Handler], Handler]: ...

    def handler(self, function: Handler | None = None, *, background: bool = True) -> Handler | Callable[[Handler], Handler]:
        if function is None:
            return lambda registered: self.add_handler(registered, background=background)
        return self.add_handler(function, background=background)

    async def dispatch_event(self, _input: Input, adapter: Adapter) -> Any:
        if self._closing:
            raise RuntimeError("Приложение останавливается")
        entries = self._matching_handlers(_input.metadata)
        payload = _input.data.model_dump(mode="python") if _input.data is not None else None
        cache: dict[type[InputData], InputData | None] = {}
        initial_state_cache: dict[str, dict[str, Any] | None] = {}
        state_cache: dict[str, dict[str, Any] | None] = {}
        for entry in entries:
            if not entry.background:
                matched, data = self._data_for_handler(entry, payload, cache)
                if not matched:
                    continue
                if not await self._matches_state(
                    adapter,
                    _input,
                    entry,
                    initial_state_cache,
                ):
                    continue
                try:
                    result = await self._invoke(
                        entry,
                        adapter,
                        _input,
                        data,
                        state_cache,
                        initial_state_cache,
                    )
                except StopHandlerChain:
                    break
                if result is not None:
                    return result

        background_entries: list[tuple[_HandlerEntry, InputData | None]] = []
        for entry in entries:
            if not entry.background:
                continue
            matched, data = self._data_for_handler(entry, payload, cache)
            if not matched:
                continue
            if not await self._matches_state(
                adapter,
                _input,
                entry,
                initial_state_cache,
            ):
                continue
            if not await self._matches_state(
                adapter,
                _input,
                entry,
                state_cache,
            ):
                continue
            background_entries.append((entry, data))
        if background_entries:
            task = asyncio.create_task(
                self._run_background_handlers(
                    tuple(background_entries),
                    adapter,
                    _input,
                    dict(state_cache),
                    dict(initial_state_cache),
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_task_done)
        return None

    async def _run_background_handlers(
        self,
        entries: tuple[tuple[_HandlerEntry, InputData | None], ...],
        adapter: Adapter,
        event: Input,
        state_cache: dict[str, dict[str, Any] | None] | None = None,
        initial_state_cache: dict[str, dict[str, Any] | None] | None = None,
    ) -> None:
        if state_cache is None:
            state_cache = {}
        for entry, data in entries:
            try:
                await self._invoke(
                    entry,
                    adapter,
                    event,
                    data,
                    state_cache,
                    initial_state_cache,
                )
            except StopHandlerChain:
                return
            except Exception:
                logger.exception(
                    "Фоновый хендлер %r завершился с ошибкой",
                    entry.function,
                )

    def _matching_handlers(self, metadata: BaseMetadata) -> tuple[_HandlerEntry, ...]:
        matched: dict[int, _HandlerEntry] = {}
        for metadata_type in type(metadata).__mro__:
            for entry in self._handlers_by_metadata.get(metadata_type, ()):
                matched[entry.order] = entry
        return tuple(matched[order] for order in sorted(matched))

    @staticmethod
    def _data_for_handler(
        entry: _HandlerEntry,
        payload: dict[str, Any] | None,
        cache: dict[type[InputData], InputData | None],
    ) -> tuple[bool, InputData | None]:
        if payload is None:
            return entry.accepts_missing_data, None

        for model in entry.data_models:
            if model in cache:
                data = cache[model]
            else:
                if not _has_required_literals(model, payload):
                    data = None
                else:
                    try:
                        data = model.model_validate(payload)
                    except ValidationError:
                        data = None
                cache[model] = data
            if data is not None:
                return True, data
        return False, None

    def _background_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        if exception := task.exception():
            logger.error("Фоновый хендлер завершился с ошибкой", exc_info=exception)

    def _adapter_start_done(self, task: asyncio.Task[None]) -> None:
        self._adapter_start_tasks.discard(task)
        if task.cancelled():
            return
        if exception := task.exception():
            logger.error("Не удалось запустить адаптер", exc_info=exception)

    async def _start_adapter(self, adapter: Adapter) -> None:
        async with self._adapter_start_lock:
            if self._closing or self._closed or adapter in self._started_adapters:
                return
            await adapter.start()
            self._started_adapters.append(adapter)

    async def _wait_for_adapter_starts(self) -> None:
        current = asyncio.current_task()
        while True:
            tasks = tuple(
                task for task in self._adapter_start_tasks if task is not current
            )
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)
            self._adapter_start_tasks.difference_update(tasks)

    async def wait_adapters_started(self) -> None:
        """Дождаться завершения запуска адаптеров, добавленных во время работы."""
        await self._wait_for_adapter_starts()

    async def wait_background(self) -> None:
        """Дождаться завершения всех запущенных фоновых хендлеров."""
        current = asyncio.current_task()
        while True:
            tasks = tuple(
                task for task in self._background_tasks if task is not current
            )
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.difference_update(tasks)

    async def _raw_context_state(
        self,
        state_key: KeyLike,
        state_cache: dict[str, dict[str, Any] | None] | None,
    ) -> dict[str, Any] | None:
        cache_key = encode_key(state_key)
        if state_cache is not None and cache_key in state_cache:
            return state_cache[cache_key]
        raw_state = await self.get_state(state_key)
        if raw_state is not None and not isinstance(raw_state, dict):
            raise TypeError("Состояние контекста должно быть JSON-объектом")
        if state_cache is not None:
            state_cache[cache_key] = raw_state
        return raw_state

    async def _matches_state(
        self,
        adapter: Adapter,
        event: Input,
        entry: _HandlerEntry,
        state_cache: dict[str, dict[str, Any] | None] | None = None,
    ) -> bool:
        if StateNamespace in entry.state_models:
            return True
        state_key = (
            event.interface.state_key()
            if event.interface is not None
            else adapter.context_key(event.metadata)
        )
        raw_state = await self._raw_context_state(state_key, state_cache)
        return self._build_state_for_handler(entry, raw_state) is not None

    @staticmethod
    def _build_state_for_handler(
        entry: _HandlerEntry,
        raw_state: dict[str, Any] | None,
    ) -> StateNamespace | None:
        if not raw_state:
            for model in entry.state_models:
                if model is StateNamespace:
                    return StateNamespace()
                if _can_instantiate_empty_state(model):
                    try:
                        return model()
                    except ValidationError:
                        continue
            if entry.accepts_empty_state:
                return StateNamespace()
            return None

        for model in entry.state_models:
            if not _has_required_literals(model, raw_state):
                continue
            try:
                return model.model_validate(raw_state)
            except ValidationError:
                continue
        return None

    async def _context_for(
        self,
        adapter: Adapter,
        event: Input,
        entry: _HandlerEntry,
        state_cache: dict[str, dict[str, Any] | None] | None = None,
        initial_state_cache: dict[str, dict[str, Any] | None] | None = None,
    ) -> Context[Any, Any] | None:
        interface = event.interface
        if interface is not None:
            state_key: KeyLike = interface.state_key()
            state_ttl: TTLValue = interface.ttl
        else:
            state_key = adapter.context_key(event.metadata)
            state_ttl = self.ttl
        cache_key = encode_key(state_key)
        raw_state = await self._raw_context_state(state_key, state_cache)
        if initial_state_cache is not None and cache_key not in initial_state_cache:
            initial_state_cache[cache_key] = raw_state
        state = self._build_state_for_handler(entry, raw_state)
        if state is None:
            return None
        return Context(
            app=self,
            adapter=adapter,
            interface=interface,
            metadata=event.metadata,
            state=state,
            _state_key=state_key,
            _state_ttl=state_ttl,
            _state_cache=state_cache,
        )

    async def _invoke(
        self,
        entry: _HandlerEntry,
        adapter: Adapter,
        event: Input,
        data: InputData | None,
        state_cache: dict[str, dict[str, Any] | None] | None = None,
        initial_state_cache: dict[str, dict[str, Any] | None] | None = None,
    ) -> Any:
        context = await self._context_for(
            adapter,
            event,
            entry,
            state_cache,
            initial_state_cache,
        )
        if context is None:
            return None
        try:
            result = entry.function(
                context,
                event.metadata,
                data,
                event.text or "",
            )
            result = await result if inspect.isawaitable(result) else result
        except StopHandlerChain:
            await context._save()
            raise
        await context._save()
        return result

    async def handle_input(self, raw: str | bytes | Mapping[str, Any]) -> Any:
        if self._closing:
            raise RuntimeError("Приложение останавливается")
        for adapter in self._adapters:
            event = await adapter.parse_input(raw)
            if event is not None:
                return await adapter.dispatch_input(event)
        return None

    async def _register_interface(
        self,
        external_id: KeyLike,
        actions: dict[str, dict[str, Any]],
        ttl: TTLValue,
        *,
        platform: str = "",
    ) -> float | None:
        interface_key = self._interface_key(external_id, platform)
        raw = await self.storage.get(interface_key)
        old_record = self._parse_interface_record(raw) if raw is not None else None
        default_ttl = old_record.ttl if old_record is not None else self.ttl
        lifetime = default_ttl if isinstance(ttl, _DefaultTTL) else ttl
        if lifetime is not None and lifetime <= 0:
            raise ValueError("ttl должен быть больше нуля или None")
        record = InterfaceRecord(dict(actions), lifetime)
        await self.storage.set(interface_key, _json(record.to_json_data()), ttl=lifetime)
        return lifetime

    async def _get_interface_record(
        self,
        external_id: KeyLike,
        *,
        platform: str = "",
    ) -> InterfaceRecord | None:
        raw = await self.storage.get(self._interface_key(external_id, platform))
        return self._parse_interface_record(raw) if raw is not None else None

    async def _remove_interface_record(
        self,
        external_id: KeyLike,
        *,
        platform: str = "",
    ) -> None:
        await self.storage.delete(self._interface_key(external_id, platform))

    @staticmethod
    def _interface_key(external_id: KeyLike, platform: str) -> str:
        if platform:
            return storage_key(
                "interface",
                {"platform": platform, "id": encode_key(external_id)},
            )
        return storage_key("interface", external_id)

    @staticmethod
    def _parse_interface_record(raw: str) -> InterfaceRecord | None:
        try:
            return InterfaceRecord.from_json_data(json.loads(raw))
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("Повреждённая запись интерфейса: %s", exc)
            return None

    @overload
    async def get_state(self, key: KeyLike, model: None = None) -> Any | None: ...

    @overload
    async def get_state(self, key: KeyLike, model: type[StateT]) -> StateT | None: ...

    async def get_state(
        self, key: KeyLike, model: type[StateT] | None = None
    ) -> Any | None:
        raw = await self.storage.get(storage_key("state", key))
        if raw is None:
            return None
        return self._deserialize_state(raw, model)

    @staticmethod
    def _deserialize_state(raw: str, model: type[StateT] | None = None) -> Any:
        if model is None:
            return json.loads(raw)
        data = json.loads(raw)
        if model is dict:
            return data
        if isinstance(model, type) and issubclass(model, BaseModel):
            return model.model_validate(data)
        if hasattr(model, "from_json"):
            return model.from_json(raw)  # type: ignore[attr-defined, no-any-return]
        return model(**data)  # type: ignore[call-arg, no-any-return]

    @staticmethod
    def _serialize_state(value: Any) -> str:
        if isinstance(value, BaseModel) or hasattr(value, "model_dump"):
            data = value.model_dump(mode="json")
        elif is_dataclass(value) and not isinstance(value, type):
            data = asdict(value)
        else:
            data = value
        return _json(data)

    async def set_state(self, key: KeyLike, value: Any, *, ttl: TTLValue = DEFAULT_TTL) -> None:
        lifetime = self.ttl if isinstance(ttl, _DefaultTTL) else ttl
        if lifetime is not None and lifetime <= 0:
            raise ValueError("ttl должен быть больше нуля или None")
        await self.storage.set(
            storage_key("state", key),
            self._serialize_state(value),
            ttl=lifetime,
        )

    async def delete_state(self, key: KeyLike) -> None:
        await self.storage.delete(storage_key("state", key))

    def lock(self, *keys: KeyLike) -> LockedKeys:
        return self._locks.lock(*keys)

    @overload
    def locked_state(
        self,
        key: KeyLike,
        model: None = None,
        *,
        ttl: TTLValue = DEFAULT_TTL,
    ) -> AbstractAsyncContextManager[Any | None]: ...

    @overload
    def locked_state(
        self,
        key: KeyLike,
        model: type[StateT],
        *,
        ttl: TTLValue = DEFAULT_TTL,
    ) -> AbstractAsyncContextManager[StateT | None]: ...

    @overload
    def locked_state(
        self,
        key: KeyLike,
        model: type[StateT],
        *,
        default: StateT | Callable[[], StateT],
        ttl: TTLValue = DEFAULT_TTL,
    ) -> AbstractAsyncContextManager[StateT]: ...

    @asynccontextmanager
    async def locked_state(
        self,
        key: KeyLike,
        model: type[StateT] | None = None,
        *,
        default: Any = _MISSING,
        ttl: TTLValue = DEFAULT_TTL,
    ) -> AsyncIterator[Any]:
        """Заблокировать ключ, загрузить состояние и сохранить изменения при выходе."""
        lifetime = self.ttl if isinstance(ttl, _DefaultTTL) else ttl
        if lifetime is not None and lifetime <= 0:
            raise ValueError("ttl должен быть больше нуля или None")
        skey = storage_key("state", key)
        async with self.lock(key):
            raw_before = await self.storage.get(skey)
            if raw_before is None:
                if default is _MISSING:
                    value = None
                elif callable(default):
                    value = default()
                elif isinstance(default, BaseModel):
                    value = default.model_copy(deep=True)
                else:
                    value = copy.deepcopy(default)
            else:
                value = self._deserialize_state(raw_before, model)
            before_serialized = (
                self._serialize_state(value) if value is not None else None
            )
            yield value
            if value is None:
                return
            after_serialized = self._serialize_state(value)
            if after_serialized == before_serialized:
                return
            if raw_before is not None and await self.storage.get(skey) is None:
                return
            await self.storage.set(skey, after_serialized, ttl=lifetime)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closing:
                raise RuntimeError("Нельзя запустить приложение во время остановки")
            if self._running:
                return
            if self._started_adapters:
                raise RuntimeError(
                    "Предыдущее закрытие не завершено; повторите close()"
                )
            adapters = tuple(self._adapters)
            self._running = True
            self._closed = False
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
        current = asyncio.current_task()
        if self._closing and (
            current is self._close_owner or current in self._background_tasks
        ):
            # Повторный close() из адаптера или фонового хендлера не должен
            # ждать close(), который сам его вызвал.
            return
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closing = True
            self._close_owner = current
            self._running = False
            try:
                try:
                    await self._wait_for_adapter_starts()
                    await self._stop_adapters()
                finally:
                    try:
                        await self._drain_background_tasks()
                    finally:
                        if self._owns_planner:
                            await self.planner.stop()
                        else:
                            await self.planner.remove(self._cleanup_task.task_id)
            finally:
                self._running = False
                self._closing = False
                self._closed = not self._started_adapters
                self._close_owner = None

    async def _drain_background_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = tuple(task for task in self._background_tasks if task is not current)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=self.close_timeout)
        if pending:
            logger.warning("Отменяю %d фоновых задач по таймауту", len(pending))
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._background_tasks.difference_update(tasks)

    async def _stop_adapters(self) -> None:
        shutdown_error: BaseException | None = None
        for adapter in tuple(reversed(self._started_adapters)):
            try:
                if self.adapter_close_timeout is None:
                    await adapter.close()
                else:
                    await asyncio.wait_for(
                        adapter.close(),
                        timeout=self.adapter_close_timeout,
                    )
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
                if shutdown_error is None:
                    shutdown_error = exc
                else:
                    shutdown_error.add_note(f"Ошибка остановки другого адаптера: {exc!r}")
            else:
                self._started_adapters.remove(adapter)
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


def _caller_localns(function: Handler) -> dict[str, Any]:
    localns: dict[str, Any] = {}
    with suppress(TypeError):
        localns.update(inspect.getclosurevars(function).nonlocals)

    frame = inspect.currentframe()
    frames = []
    try:
        frame = frame.f_back if frame is not None else None
        while frame is not None:
            module = frame.f_globals.get("__name__", "")
            if module != "vkuxt" and not module.startswith("vkuxt."):
                frames.append(frame)
            frame = frame.f_back
        for external_frame in reversed(frames):
            localns.update(external_frame.f_locals)
    finally:
        del frame
    return localns


def _handler_annotations(
    function: Handler,
    localns: dict[str, Any],
) -> tuple[
    tuple[type[BaseMetadata], ...],
    tuple[type[InputData], ...],
    bool,
    tuple[type[StateNamespace], ...],
    bool,
]:
    try:
        all_parameters = list(inspect.signature(function).parameters.values())
        parameters = [
            parameter
            for parameter in all_parameters
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError) as exc:
        raise TypeError("Не удалось прочитать сигнатуру хендлера") from exc
    if len(all_parameters) != 4 or len(parameters) != 4:
        raise TypeError(
            "Хендлер должен принимать ровно четыре позиционных аргумента: "
            "ctx, metadata, data, text"
        )

    globalns = getattr(function, "__globals__", {})
    try:
        hints = get_type_hints(function, globalns=globalns, localns=localns)
    except (NameError, TypeError) as exc:
        raise TypeError(
            f"Не удалось разрешить аннотации хендлера "
            f"{getattr(function, '__qualname__', function)!r}"
        ) from exc

    metadata_annotation = hints.get(parameters[1].name, parameters[1].annotation)
    data_annotation = hints.get(parameters[2].name, parameters[2].annotation)
    text_annotation = hints.get(parameters[3].name, parameters[3].annotation)
    ctx_annotation = _unwrap_annotation(
        hints.get(parameters[0].name, parameters[0].annotation)
    )
    ctx_type = get_origin(ctx_annotation) or ctx_annotation
    if ctx_type not in (inspect.Signature.empty, Any) and not (
        isinstance(ctx_type, type) and issubclass(ctx_type, Context)
    ):
        raise TypeError("Аннотация ctx должна быть Context")
    state_models, accepts_empty_state = _context_state_types(ctx_annotation, ctx_type)

    metadata_types = _metadata_annotation_types(metadata_annotation)
    data_models, accepts_missing_data = _data_annotation_types(data_annotation)
    if _unwrap_annotation(text_annotation) is not str:
        raise TypeError("Аннотация text должна быть str")
    return (
        metadata_types,
        data_models,
        accepts_missing_data,
        state_models,
        accepts_empty_state,
    )


def _context_state_types(
    annotation: Any,
    ctx_type: Any,
) -> tuple[tuple[type[StateNamespace], ...], bool]:
    if ctx_type in (inspect.Signature.empty, Any) or ctx_type is not Context:
        return (StateNamespace,), True
    arguments = get_args(annotation)
    if len(arguments) > 2:
        raise TypeError(
            "Context принимает не больше двух параметров: адаптер и тип состояния"
        )
    if len(arguments) < 2:
        return (StateNamespace,), True
    state_annotation = _unwrap_annotation(arguments[1])
    if state_annotation is Any:
        return (StateNamespace,), True
    if isinstance(state_annotation, (str, ForwardRef)):
        raise TypeError(f"Не удалось разрешить аннотацию state {state_annotation!r}")
    state_models: list[type[StateNamespace]] = []
    accepts_empty_state = False
    for state_type in _type_alternatives(state_annotation):
        if state_type is type(None):
            accepts_empty_state = True
        elif isinstance(state_type, type) and issubclass(state_type, StateNamespace):
            if state_type not in state_models:
                state_models.append(state_type)
        else:
            raise TypeError(
                "Второй параметр Context должен наследоваться от StateNamespace или быть None"
            )
    return tuple(state_models), accepts_empty_state


def _is_non_null_literal(annotation: Any) -> bool:
    annotation = _unwrap_annotation(annotation)
    if get_origin(annotation) is Literal:
        return all(arg is not None for arg in get_args(annotation))
    if get_origin(annotation) in (Union, types.UnionType):
        alternatives = _type_alternatives(annotation)
        return bool(alternatives) and all(
            get_origin(alt) is Literal and all(arg is not None for arg in get_args(alt))
            for alt in alternatives
        )
    return False


def _required_literal_fields(
    model: type[BaseModel],
) -> tuple[tuple[str, str | None], ...]:
    fields: list[tuple[str, str | None]] = []
    for name, field_info in model.model_fields.items():
        if _is_non_null_literal(field_info.annotation):
            fields.append((name, field_info.alias))
    return tuple(fields)


def _has_required_literals(
    model: type[BaseModel],
    raw: Mapping[str, Any],
) -> bool:
    for name, alias in _required_literal_fields(model):
        if name not in raw and (alias is None or alias not in raw):
            return False
    return True


def _can_instantiate_empty_state(model: type[StateNamespace]) -> bool:
    if model is StateNamespace:
        return True
    if _required_literal_fields(model):
        return False
    return not any(field.is_required() for field in model.model_fields.values())


def _type_param_default(param: Any) -> Any:
    default = getattr(param, "__default__", NoDefault)
    if default is not NoDefault:
        return default
    bound = getattr(param, "__bound__", None)
    return bound if bound is not None else Any


def _expand_type_alias(annotation: Any) -> Any:
    while True:
        if isinstance(annotation, TypeAliasType):
            if annotation.__type_params__:
                defaults = tuple(
                    _type_param_default(param)
                    for param in annotation.__type_params__
                )
                annotation = annotation.__value__[defaults]
            else:
                annotation = annotation.__value__
            continue
        origin = get_origin(annotation)
        if isinstance(origin, TypeAliasType):
            args = get_args(annotation)
            if len(args) < len(origin.__type_params__):
                args = args + tuple(
                    _type_param_default(param)
                    for param in origin.__type_params__[len(args) :]
                )
            annotation = origin.__value__[args]
            continue
        if isinstance(annotation, TypeVar):
            annotation = _type_param_default(annotation)
            continue
        break
    return annotation


def _unwrap_annotation(annotation: Any) -> Any:
    annotation = _expand_type_alias(annotation)
    while get_origin(annotation) is Annotated:
        annotation = _expand_type_alias(get_args(annotation)[0])
    return annotation


def _type_alternatives(annotation: Any) -> tuple[Any, ...]:
    annotation = _expand_type_alias(annotation)
    if get_origin(annotation) is Annotated:
        return _type_alternatives(get_args(annotation)[0])
    if get_origin(annotation) in (Union, types.UnionType):
        return tuple(
            alternative
            for member in get_args(annotation)
            for alternative in _type_alternatives(member)
        )
    return (annotation,)


def _metadata_annotation_types(annotation: Any) -> tuple[type[BaseMetadata], ...]:
    annotation = _unwrap_annotation(annotation)
    if annotation is inspect.Signature.empty:
        raise TypeError("Аннотация metadata обязательна")
    if isinstance(annotation, (str, ForwardRef)):
        raise TypeError(
            f"Не удалось разрешить аннотацию metadata {annotation!r}"
        )
    if annotation in (Any, object):
        raise TypeError(
            "Аннотация metadata должна указывать BaseMetadata или Union моделей metadata"
        )

    metadata_types: list[type[BaseMetadata]] = []
    for metadata_type in _type_alternatives(annotation):
        if not isinstance(metadata_type, type) or not issubclass(metadata_type, BaseMetadata):
            raise TypeError("Типы metadata должны наследоваться от BaseMetadata")
        if metadata_type not in metadata_types:
            metadata_types.append(metadata_type)
    return tuple(metadata_types)


def _data_annotation_types(
    annotation: Any,
) -> tuple[tuple[type[InputData], ...], bool]:
    annotation = _unwrap_annotation(annotation)
    if annotation is inspect.Signature.empty:
        raise TypeError("Аннотация data обязательна")
    if isinstance(annotation, (str, ForwardRef)):
        raise TypeError(f"Не удалось разрешить аннотацию data {annotation!r}")
    if annotation in (Any, object):
        raise TypeError(
            "Аннотация data должна указывать InputData, None или Union моделей"
        )
    data_models: list[type[InputData]] = []
    accepts_missing_data = False
    for data_type in _type_alternatives(annotation):
        if data_type is type(None):
            accepts_missing_data = True
        elif isinstance(data_type, type) and issubclass(data_type, InputData):
            if data_type not in data_models:
                data_models.append(data_type)
        else:
            raise TypeError("Типы data должны быть моделями InputData или None")
    return tuple(data_models), accepts_missing_data


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Данные не сериализуются в JSON: {exc}") from exc
