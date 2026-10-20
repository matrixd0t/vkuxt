from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class _Item:
    value: str
    expires_at: float | None


class StateStorage(ABC):
    """Абстрактный базовый класс асинхронных хранилищ состояния."""

    @abstractmethod
    async def get(self, key: str) -> str | None: ...

    @abstractmethod
    async def set(self, key: str, value: str, *, ttl: float | None = None) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def cleanup_expired(self) -> None: ...


class MemoryStorage(StateStorage):
    """Хранилище в памяти для тестов и локальной разработки."""

    def __init__(self) -> None:
        self._items: dict[str, _Item] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            item = self._items.get(key)
            if item is not None and _expired(item):
                self._items.pop(key, None)
                return None
            return item.value if item is not None else None

    async def set(self, key: str, value: str, *, ttl: float | None = None) -> None:
        expires_at = _expiration(ttl)
        async with self._lock:
            self._items[key] = _Item(value, expires_at)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._items.pop(key, None)

    async def cleanup_expired(self) -> None:
        async with self._lock:
            self._cleanup_locked()

    def _cleanup_locked(self) -> None:
        now = time.time()
        expired = [
            key
            for key, item in self._items.items()
            if item.expires_at is not None and item.expires_at <= now
        ]
        for key in expired:
            del self._items[key]


class JSONFileStorage(StateStorage):
    """Однопроцессное JSON-хранилище с атомарной заменой файла.

    Запись выполняется в отдельном потоке, чтобы не блокировать event loop.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, _Item] = {}
        self._lock = asyncio.Lock()
        self._read_file()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            item = self._items.get(key)
            if item is not None and _expired(item):
                self._items.pop(key, None)
                await self._write_file_async()
                return None
            return item.value if item is not None else None

    async def set(self, key: str, value: str, *, ttl: float | None = None) -> None:
        async with self._lock:
            self._items[key] = _Item(value, _expiration(ttl))
            await self._write_file_async()

    async def delete(self, key: str) -> None:
        async with self._lock:
            if self._items.pop(key, None) is not None:
                await self._write_file_async()

    async def cleanup_expired(self) -> None:
        async with self._lock:
            await self._cleanup_locked()

    async def _cleanup_locked(self) -> None:
        now = time.time()
        expired = [
            key
            for key, item in self._items.items()
            if item.expires_at is not None and item.expires_at <= now
        ]
        if expired:
            for key in expired:
                del self._items[key]
            await self._write_file_async()

    async def _write_file_async(self) -> None:
        task = asyncio.create_task(asyncio.to_thread(self._write_file))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Отмена не должна оставлять запись в фоне: иначе следующий
            # set() может писать тот же временный файл одновременно.
            await asyncio.gather(task, return_exceptions=True)
            raise

    def _read_file(self) -> None:
        if not self.path.exists():
            return
        text = self.path.read_text(encoding="utf-8")
        if not text.strip():
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Файл состояния {self.path} содержит некорректный JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Файл состояния {self.path} должен содержать JSON-объект")
        items: dict[str, _Item] = {}
        try:
            for key, value in data.items():
                if not isinstance(value, dict):
                    raise TypeError("элемент должен быть объектом")
                raw_value = value.get("value")
                if not isinstance(raw_value, str):
                    raise TypeError("поле value должно быть строкой")
                expires_at = value.get("expires_at")
                if expires_at is not None and not isinstance(expires_at, (int, float)):
                    raise TypeError("поле expires_at должно быть числом или null")
                items[key] = _Item(raw_value, expires_at)
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"Файл состояния {self.path} повреждён: {exc}") from exc
        self._items = items

    def _write_file(self) -> None:
        data = {
            key: {"value": item.value, "expires_at": item.expires_at}
            for key, item in self._items.items()
        }
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def _expiration(ttl: float | None) -> float | None:
    if ttl is None:
        return None
    if ttl <= 0:
        raise ValueError("ttl должен быть больше нуля или None")
    return time.time() + ttl


def _expired(item: _Item) -> bool:
    return item.expires_at is not None and item.expires_at <= time.time()
