from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .codec import KeyLike, encode_key


class _DefaultTTL:
    pass


DEFAULT_TTL = _DefaultTTL()
TTLValue = float | None | _DefaultTTL


@dataclass(slots=True)
class Interface[ButtonT]:
    """Общий контракт отрисовываемого интерфейса: текст и двумерный список кнопок.

    Базовый класс не знает, чем являются кнопки. Адаптер объявляет свой
    подкласс через ``Adapter.interface_type`` и при необходимости валидирует
    транспортные ограничения.
    """

    text: str = ""
    kb: Sequence[Sequence[ButtonT]] = ()
    ttl: TTLValue = DEFAULT_TTL

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Проверить контракт; адаптеры могут вызывать перед отрисовкой."""
        if not isinstance(self.text, str):
            raise TypeError("text интерфейса должен быть строкой")
        normalized_rows: list[tuple[ButtonT, ...]] = []
        for index, row in enumerate(self.kb):
            if isinstance(row, (str, bytes, bytearray)):
                raise TypeError(f"ряд кнопок {index} должен быть последовательностью, а не строкой")
            normalized_rows.append(tuple(row))
        self.kb = tuple(normalized_rows)
        if (
            not isinstance(self.ttl, _DefaultTTL)
            and self.ttl is not None
            and (not isinstance(self.ttl, (int, float)) or self.ttl <= 0)
        ):
            raise ValueError("ttl интерфейса должен быть больше нуля или None")


@dataclass(slots=True)
class InterfaceHandle[IdT: KeyLike]:
    """Python-представление конкретного удалённого интерфейса.

    ``id`` остаётся деталью адаптера и не нужен прикладному коду для
    перерисовки: объект передаётся в ``Adapter.redraw()`` целиком.
    """

    id: IdT
    actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    ttl: float | None = None
    platform: str = ""
    deleted: bool = False
    """Установлен адаптером после успешного удаления интерфейса."""

    def state_key(self) -> dict[str, Any]:
        """Стабильный ключ для ``App.get_state``/``set_state``.

        Зависит от ``id`` и ``platform`` и не меняется при перерисовках. Сам
        handle ключом не является: ``actions`` и ``ttl`` обновляются адаптером.
        """
        if self.platform:
            return {
                "interface": {
                    "platform": self.platform,
                    "id": encode_key(self.id),
                }
            }
        return {"interface": encode_key(self.id)}


@dataclass(slots=True)
class InterfaceRecord:
    actions: dict[str, dict[str, Any]]
    ttl: float | None

    def to_json_data(self) -> dict[str, Any]:
        return {"actions": self.actions, "ttl": self.ttl}

    @classmethod
    def from_json_data(cls, data: dict[str, Any]) -> InterfaceRecord:
        actions = data.get("actions", {})
        if not isinstance(actions, dict):
            raise ValueError("Поле actions интерфейса должно быть объектом")
        for token, action in actions.items():
            if not isinstance(token, str):
                raise ValueError("Ключи actions интерфейса должны быть строками")
            if not isinstance(action, dict):
                raise ValueError("Значения actions интерфейса должны быть объектами")
            if "data" not in action:
                raise ValueError("Значение actions интерфейса должно содержать data")
        ttl = data.get("ttl")
        if ttl is not None and (not isinstance(ttl, (int, float)) or ttl <= 0):
            raise ValueError("TTL интерфейса должен быть положительным числом или None")
        return cls(actions=actions, ttl=ttl)
