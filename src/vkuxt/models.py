from __future__ import annotations

import json
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator

_MISSING = object()


class InputModel(BaseModel):
    """Валидированная модель входа; подклассы могут переопределить from_json."""

    @classmethod
    def from_json(cls, raw_json: str) -> Self:
        return cls.model_validate_json(raw_json)


class _JSONObjectModel(InputModel):
    """База изменяемых JSON-объектов: extra-поля и строгая сериализация."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_") or name in type(self).model_fields:
            super().__setattr__(name, value)
            return
        extra = self.__pydantic_extra__
        previous = extra.get(name, _MISSING) if extra is not None else _MISSING
        try:
            super().__setattr__(name, value)
        except Exception:
            current = self.__pydantic_extra__
            if current is not None:
                if previous is _MISSING:
                    current.pop(name, None)
                else:
                    current[name] = previous
            raise

    @model_validator(mode="after")
    def _ensure_json_serializable(self) -> Self:
        try:
            json.dumps(
                self.model_dump(mode="json", warnings="error"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except Exception as exc:
            raise ValueError(
                f"{type(self).__name__} должен сериализоваться в строгий JSON"
            ) from exc
        return self


class InputData(_JSONObjectModel):
    """База моделей типизированных данных, принимаемых хендлерами."""


class StateNamespace(_JSONObjectModel):
    """Изменяемый namespace состояния контекста.

    Поля подкласса видны IDE и валидируются при присваивании. Необъявленные
    атрибуты разрешены и сохраняются как extra-поля. Значения обязаны
    сериализоваться в строгий JSON.
    """


class BaseMetadata(InputModel):
    """Базовый тип для метаданных, предоставляемых адаптерами."""
