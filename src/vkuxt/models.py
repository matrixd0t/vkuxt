from __future__ import annotations

import json
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator


class InputModel(BaseModel):
    """Валидированная модель входа; подклассы могут переопределить from_json."""

    @classmethod
    def from_json(cls, raw_json: str) -> Self:
        return cls.model_validate_json(raw_json)


class InputData(InputModel):
    """База моделей типизированных данных, принимаемых хендлерами."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    @model_validator(mode="after")
    def _ensure_json_serializable(self) -> Self:
        try:
            json.dumps(
                self.model_dump(mode="json", warnings="error"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except Exception as exc:
            raise ValueError("InputData должен сериализоваться в строгий JSON") from exc
        return self


class BaseMetadata(InputModel):
    """Базовый тип для метаданных, предоставляемых адаптерами."""
