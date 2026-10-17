from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .codec import KeyLike, encode_key


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
    """Локирует ключи в каноническом порядке, предотвращая взаимоблокировки.

    Вложенный захват допустим, только если новые ключи сортируются позже всех
    уже удерживаемых этим же task'ом. Иначе task ждал бы ключ, который может
    быть занят другим task'ом, ожидающим уже удерживаемый ключ.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _LockEntry] = {}
        self._guard = asyncio.Lock()
        self._held_by_task: dict[asyncio.Task[Any], dict[str, int]] = {}

    def lock(self, *keys: KeyLike) -> LockedKeys:
        if not keys:
            raise ValueError("Нужно передать хотя бы один ключ")
        return LockedKeys(self, tuple(sorted({encode_key(key) for key in keys})))

    async def _reserve(self, keys: tuple[str, ...]) -> list[_LockEntry]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Keyed lock должен использоваться из asyncio.Task")
        held = self._held_by_task.get(task)
        if held:
            new_keys = [key for key in keys if not held.get(key)]
            if new_keys and min(new_keys) < max(held):
                raise RuntimeError(
                    "Нельзя захватывать ключ, который сортируется раньше уже удерживаемого: "
                    "это может привести к взаимоблокировке"
                )

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

    def _mark_held(self, task: asyncio.Task[Any], key: str) -> None:
        counts = self._held_by_task.setdefault(task, {})
        counts[key] = counts.get(key, 0) + 1

    def _unmark_held(self, task: asyncio.Task[Any], key: str) -> None:
        counts = self._held_by_task.get(task)
        if counts is None:
            return
        remaining = counts.get(key, 0) - 1
        if remaining > 0:
            counts[key] = remaining
        else:
            counts.pop(key, None)
            if not counts:
                self._held_by_task.pop(task, None)


class LockedKeys:
    def __init__(self, pool: KeyedLockPool, keys: tuple[str, ...]) -> None:
        self._pool = pool
        self._keys = keys
        self._entries: list[_LockEntry] = []
        self._acquired: list[tuple[str, _ReentrantAsyncLock]] = []

    async def __aenter__(self) -> LockedKeys:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Keyed lock должен использоваться из asyncio.Task")
        self._entries = await self._pool._reserve(self._keys)
        try:
            for key, entry in zip(self._keys, self._entries, strict=True):
                await entry.lock.acquire()
                self._acquired.append((key, entry.lock))
                self._pool._mark_held(task, key)
        except BaseException:
            self._release(task)
            await self._pool._release_reservations(self._keys, self._entries)
            raise
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Keyed lock должен использоваться из asyncio.Task")
        self._release(task)
        await self._pool._release_reservations(self._keys, self._entries)

    def _release(self, task: asyncio.Task[Any]) -> None:
        for key, lock in reversed(self._acquired):
            lock.release()
            self._pool._unmark_held(task, key)
        self._acquired.clear()
