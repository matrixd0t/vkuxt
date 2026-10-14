from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .codec import encode_key


class _ReentrantAsyncLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Keyed lock должен использоваться из asyncio.Task")
        if self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        task = asyncio.current_task()
        if task is None or self._owner is not task:
            raise RuntimeError("Keyed lock освобождается не владельцем")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


@dataclass(slots=True)
class _LockEntry:
    lock: _ReentrantAsyncLock
    users: int = 0


class KeyedLockPool:
    """Локирует ключи в каноническом порядке, предотвращая взаимоблокировки."""

    def __init__(self) -> None:
        self._entries: dict[str, _LockEntry] = {}
        self._guard = asyncio.Lock()

    def lock(self, *keys: Any) -> _LockedKeys:
        if not keys:
            raise ValueError("Нужно передать хотя бы один ключ")
        return _LockedKeys(self, tuple(sorted({encode_key(key) for key in keys})))

    async def _reserve(self, keys: tuple[str, ...]) -> list[_LockEntry]:
        async with self._guard:
            entries = []
            for key in keys:
                entry = self._entries.setdefault(key, _LockEntry(_ReentrantAsyncLock()))
                entry.users += 1
                entries.append(entry)
            return entries

    async def _release_reservations(self, keys: tuple[str, ...], entries: list[_LockEntry]) -> None:
        async with self._guard:
            for key, entry in zip(keys, entries, strict=True):
                entry.users -= 1
                if entry.users == 0:
                    self._entries.pop(key, None)


class _LockedKeys:
    def __init__(self, pool: KeyedLockPool, keys: tuple[str, ...]) -> None:
        self._pool = pool
        self._keys = keys
        self._entries: list[_LockEntry] = []
        self._acquired: list[_ReentrantAsyncLock] = []

    async def __aenter__(self) -> _LockedKeys:
        self._entries = await self._pool._reserve(self._keys)
        try:
            for entry in self._entries:
                await entry.lock.acquire()
                self._acquired.append(entry.lock)
        except BaseException:
            for lock in reversed(self._acquired):
                lock.release()
            await self._pool._release_reservations(self._keys, self._entries)
            raise
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        for lock in reversed(self._acquired):
            lock.release()
        await self._pool._release_reservations(self._keys, self._entries)
