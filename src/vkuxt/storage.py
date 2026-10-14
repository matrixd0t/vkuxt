from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class StorageItem:
    key: str
    value: str
    expires_at: float | None


@runtime_checkable
class StateStorage(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, *, ttl: float | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...


class MemoryStorage:
    def __init__(self) -> None:
        self._items: dict[str, StorageItem] = {}
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
            self._items[key] = StorageItem(key, value, expires_at)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._items.pop(key, None)

    async def scan(self, prefix: str) -> list[StorageItem]:
        async with self._lock:
            await self._cleanup_locked()
            return [item for key, item in self._items.items() if key.startswith(prefix)]

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
        for key in expired:
            del self._items[key]


class JSONFileStorage:
    """Однопроцессное JSON-хранилище с атомарной заменой файла."""  # noqa: RUF002

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, StorageItem] = {}
        self._lock = asyncio.Lock()
        self._read_file()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            item = self._items.get(key)
            if item is not None and _expired(item):
                self._items.pop(key, None)
                self._write_file()
                return None
            return item.value if item is not None else None

    async def set(self, key: str, value: str, *, ttl: float | None = None) -> None:
        async with self._lock:
            self._items[key] = StorageItem(key, value, _expiration(ttl))
            self._write_file()

    async def delete(self, key: str) -> None:
        async with self._lock:
            if self._items.pop(key, None) is not None:
                self._write_file()

    async def scan(self, prefix: str) -> list[StorageItem]:
        async with self._lock:
            await self._cleanup_locked()
            return [item for key, item in self._items.items() if key.startswith(prefix)]

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
            self._write_file()

    def _read_file(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._items = {
            key: StorageItem(key, value["value"], value.get("expires_at"))
            for key, value in data.items()
        }

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


class RedisStorage:
    """Адаптер для async Redis-клиента без жёсткой зависимости на redis-py."""

    def __init__(self, client: object) -> None:
        self.client = client

    async def get(self, key: str) -> str | None:
        value = await self.client.get(key)  # type: ignore[attr-defined]
        if value is None:
            return None
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    async def set(self, key: str, value: str, *, ttl: float | None = None) -> None:
        if ttl is None:
            await self.client.set(key, value)  # type: ignore[attr-defined]
        else:
            if ttl <= 0:
                raise ValueError("ttl должен быть больше нуля или None")
            await self.client.set(key, value, px=max(1, round(ttl * 1000)))  # type: ignore[attr-defined]

    async def delete(self, key: str) -> None:
        await self.client.delete(key)  # type: ignore[attr-defined]

    async def scan(self, prefix: str) -> list[StorageItem]:
        result: list[StorageItem] = []
        async for raw_key in self.client.scan_iter(match=f"{prefix}*"):  # type: ignore[attr-defined]
            key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
            value = await self.get(key)
            if value is None:
                continue
            ttl_ms = await self.client.pttl(key)  # type: ignore[attr-defined]
            expires_at = time.time() + ttl_ms / 1000 if ttl_ms >= 0 else None
            result.append(StorageItem(key, value, expires_at))
        return result


def _expiration(ttl: float | None) -> float | None:
    if ttl is None:
        return None
    if ttl <= 0:
        raise ValueError("ttl должен быть больше нуля или None")
    return time.time() + ttl


def _expired(item: StorageItem) -> bool:
    return item.expires_at is not None and item.expires_at <= time.time()
