from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


class _DefaultTTL:
    pass


DEFAULT_TTL = _DefaultTTL()
TTLValue = float | None | _DefaultTTL


@dataclass(slots=True)
class InterfaceDraft:
    ttl: TTLValue = DEFAULT_TTL
    tokenize_long_input: bool = True
    actions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def bind(
        self,
        data: BaseModel | dict[str, Any],
    ) -> str | dict[str, Any]:
        from .app import InputData

        if isinstance(data, InputData):
            input_data = data.model_dump(mode="json")
        elif isinstance(data, dict):
            input_data = data
        else:
            raise TypeError("Структурированные данные должны быть InputData или словарём")
        encoded = json.dumps(
            input_data,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        if not self.tokenize_long_input or len(encoded) <= 255:
            return input_data
        token = secrets.token_urlsafe(6)
        while token in self.actions:
            token = secrets.token_urlsafe(6)
        self.actions[token] = {"data": input_data}
        return token


@dataclass(slots=True)
class InterfaceRecord:
    actions: dict[str, dict[str, Any]]
    ttl: float | None

    def to_json_data(self) -> dict[str, Any]:
        return {"actions": self.actions, "ttl": self.ttl}

    @classmethod
    def from_json_data(cls, data: dict[str, Any]) -> InterfaceRecord:
        return cls(
            actions=data.get("actions", {}),
            ttl=data.get("ttl"),
        )
