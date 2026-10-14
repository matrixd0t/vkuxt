from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import UnionType
from typing import Any, Self, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from matrix_planner import Planner, every
from pydantic import BaseModel, ConfigDict, model_validator

from .codec import storage_key
from .locks import KeyedLockPool
from .mounts import DEFAULT_TTL, InterfaceDraft, InterfaceRecord, TTLValue, _DefaultTTL
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
    handler_name: str | None = None


class EventMetadata(InputModel):
    """Метаданные события, специфичные для адаптера."""


@dataclass(frozen=True, slots=True)
class AdapterInput:
    metadata: EventMetadata | None
    input: str | None = None
    data: InputData | Mapping[str, Any] | None = None
    response: Any = "ok"
    acknowledge: bool = False

    def __post_init__(self) -> None:
        if self.input is not None and self.data is not None:
            raise ValueError("Provide either input or data, not both")


class Adapter:
    def __init__(self, app: App) -> None:
        self.app = app

    async def parse_input(
        self,
        raw: str | bytes | Mapping[str, Any],
    ) -> AdapterInput | None:
        """Распознать сырой ввод или вернуть None, если формат неизвестен."""

    async def dispatch_input(self, event: AdapterInput) -> Any:
        if event.metadata is not None:
            await self.app.dispatch_event(
                event.metadata,
                input=event.input,
                data=event.data,
            )
        return event.response

    async def start(self) -> None:
        """Запустить приём событий или фоновый опрос адаптера."""

    async def close(self) -> None:
        """Остановить фоновые задачи адаптера."""


@dataclass(frozen=True, slots=True)
class _HandlerEntry:
    metadata_models: tuple[type[EventMetadata], ...]
    takes_input: bool
    takes_data: bool
    data_models: tuple[type[InputData], ...]
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
        self._locks = KeyedLockPool()
        self.services: dict[str, Any] = {}
        self._adapters: list[Adapter] = []
        self._started_adapters: list[Adapter] = []
        self._running = False
        cleanup = getattr(self.storage, "cleanup_expired", None)
        self._cleanup_task = (
            self.planner.task(
                every("1m"),
                name=f"vkuxt-storage-cleanup-{id(self)}",
            )(cleanup)
            if cleanup is not None
            else None
        )

    @property
    def adapters(self) -> tuple[Adapter, ...]:
        return tuple(self._adapters)

    def add_adapter(
        self,
        adapter_type: type[AdapterT],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> AdapterT:
        if self._running:
            raise RuntimeError("Нельзя добавлять адаптер после запуска приложения")
        adapter = adapter_type(self, *args, **kwargs)
        self._adapters.append(adapter)
        return adapter

    def add_handler(self, function: Handler) -> Handler:
        signature = inspect.signature(function)
        parameters = tuple(signature.parameters.values())
        parameter_names = tuple(parameter.name for parameter in parameters)
        allowed_names = {
            ("app", "meta"),
            ("app", "meta", "input"),
            ("app", "meta", "data"),
            ("app", "meta", "input", "data"),
        }
        if parameter_names not in allowed_names or any(
            parameter.kind != inspect.Parameter.POSITIONAL_OR_KEYWORD
            for parameter in parameters
        ):
            raise TypeError(
                "Handler signature must be (app, meta[, input][, data]) "
                "with input before data"
            )

        frame = inspect.currentframe()
        callers = []
        caller = frame.f_back if frame is not None else None
        while caller is not None:
            callers.append(caller)
            caller = caller.f_back
        localns: dict[str, Any] = {}
        for caller in reversed(callers):
            localns.update(caller.f_locals)
        try:
            hints = get_type_hints(function, localns=localns)
        finally:
            del frame
            del caller
            del callers

        if hints.get("app") is not App:
            raise TypeError("Первый аргумент обработчика должен иметь тип App")
        if "meta" not in hints:
            raise TypeError("Аргумент meta должен иметь аннотацию EventMetadata")
        metadata_models = _annotation_options(hints["meta"], EventMetadata)
        takes_input = "input" in parameter_names
        takes_data = "data" in parameter_names
        if takes_input and "input" not in hints:
            raise TypeError("Аргумент input должен иметь аннотацию str")
        if takes_input:
            _validate_input_annotation(hints["input"])
        data_models: tuple[type[InputData], ...] = ()
        if takes_data:
            if "data" not in hints:
                raise TypeError("Аргумент data должен быть моделью InputData")
            data_models = _annotation_options(hints["data"], InputData, allow_none=True)
        if takes_input and takes_data:
            for name in ("input", "data"):
                parameter = signature.parameters[name]
                if parameter.default is not None or type(None) not in _union_members(hints[name]):
                    raise TypeError(
                        "When both input and data are declared, each must be Optional "
                        "with default None"
                    )

        self._handlers.append(
            _HandlerEntry(metadata_models, takes_input, takes_data, data_models, function)
        )
        return function

    def handler(self, function: Handler) -> Handler:
        return self.add_handler(function)

    async def dispatch_event(
        self,
        metadata: EventMetadata,
        *,
        input: str | None = None,
        data: InputData | Mapping[str, Any] | None = None,
    ) -> list[Any]:
        if input is not None and data is not None:
            raise ValueError("Событие не может одновременно содержать input и data")
        results = []
        for entry in tuple(self._handlers):
            if not _accepts_metadata(entry, metadata):
                continue
            handler_input = None
            handler_data = None
            if not entry.takes_input and not entry.takes_data:
                pass
            elif entry.takes_input and entry.takes_data:
                if input is not None:
                    handler_input = input
                elif data is not None:
                    handler_data = _matching_data(entry, data)
                    if handler_data is _NO_MATCH:
                        continue
                    handler_data = cast(InputData, handler_data)
            elif input is not None and entry.takes_input:
                handler_input = input
            elif data is not None and entry.takes_data:
                handler_data = _matching_data(entry, data)
                if handler_data is _NO_MATCH:
                    continue
                handler_data = cast(InputData, handler_data)
            else:
                continue
            results.append(
                await self._invoke(entry, metadata, input=handler_input, data=handler_data)
            )
        return results

    async def _invoke(
        self,
        entry: _HandlerEntry,
        metadata: EventMetadata,
        *,
        input: str | None,
        data: InputData | None,
    ) -> Any:
        arguments: dict[str, Any] = {}
        if entry.takes_input:
            arguments["input"] = input
        if entry.takes_data:
            arguments["data"] = data
        result = entry.function(self, metadata, **arguments)
        return await result if inspect.isawaitable(result) else result

    async def handle_input(self, raw: str | bytes | Mapping[str, Any]) -> Any:
        for adapter in self._adapters:
            event = await adapter.parse_input(raw)
            if event is not None:
                return await adapter.dispatch_input(event)
        return None

    def new_interface(
        self,
        *,
        ttl: TTLValue = DEFAULT_TTL,
    ) -> InterfaceDraft:
        return InterfaceDraft(ttl=ttl, tokenize_long_input=self.tokenize_long_input)

    async def register_interface(self, external_id: Any, interface: InterfaceDraft) -> None:
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
        if self._running:
            return
        self._running = True
        try:
            if self._owns_planner and self._cleanup_task is not None:
                await self.planner.start()
            for adapter in self._adapters:
                self._started_adapters.append(adapter)
                await adapter.start()
        except (Exception, asyncio.CancelledError) as startup_error:
            try:
                await self._stop_adapters()
            except (Exception, asyncio.CancelledError) as close_error:  # noqa: BLE001
                startup_error.add_note(
                    f"Ошибка остановки адаптера после сбоя запуска: {close_error!r}"
                )
            finally:
                if self._cleanup_task is not None:
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
        try:
            await self._stop_adapters()
        finally:
            try:
                if self._cleanup_task is not None:
                    if self._owns_planner:
                        await self.planner.stop()
                    else:
                        await self.planner.remove(self._cleanup_task.task_id)
            finally:
                self._running = False

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


_NO_MATCH = object()


def _union_members(annotation: Any) -> tuple[Any, ...]:
    if get_origin(annotation) in (Union, UnionType):
        return get_args(annotation)
    return (annotation,)


def _annotation_options(
    annotation: Any,
    base: type[Any],
    *,
    allow_none: bool = False,
) -> tuple[type[Any], ...]:
    options = _union_members(annotation)
    models = tuple(
        option
        for option in options
        if isinstance(option, type) and issubclass(option, base)
    )
    has_invalid = any(
        option is not type(None)
        and (not isinstance(option, type) or not issubclass(option, base))
        for option in options
    )
    if not models or has_invalid or (not allow_none and type(None) in options):
        raise TypeError(f"Аннотация должна содержать модели-наследники {base.__name__}")
    return cast(tuple[type[Any], ...], models)


def _validate_input_annotation(annotation: Any) -> None:
    options = _union_members(annotation)
    if str not in options or any(option not in (str, type(None)) for option in options):
        raise TypeError("Аргумент input должен иметь тип str, допускается Optional при input/data")


def _accepts_metadata(entry: _HandlerEntry, metadata: EventMetadata) -> bool:
    return any(isinstance(metadata, model) for model in entry.metadata_models)


def _matching_data(
    entry: _HandlerEntry,
    value: InputData | Mapping[str, Any],
) -> InputData | object:
    raw_data = value.model_dump(mode="python") if isinstance(value, InputData) else value
    for model in entry.data_models:
        if isinstance(value, model):
            return cast(InputData, value)
        try:
            return model.model_validate(raw_data)
        except (TypeError, ValueError):
            continue
    return _NO_MATCH


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Данные не сериализуются в JSON: {exc}") from exc
