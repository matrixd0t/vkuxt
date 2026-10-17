from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any, Protocol

from pydantic import BaseModel

KEY_NAMESPACE_VERSION = "v1"


class DataclassKey(Protocol):
    __dataclass_fields__: Mapping[str, Any]


KeyLike = Mapping[str, Any] | BaseModel | DataclassKey


def key_data(key: KeyLike) -> dict[str, Any]:
    if isinstance(key, BaseModel):
        value = key.model_dump(mode="json", exclude_none=False)
    elif is_dataclass(key) and not isinstance(key, type):
        value = asdict(key)
    elif isinstance(key, Mapping):
        value = dict(key)
    else:
        raise TypeError("Ключ состояния должен быть mapping, dataclass или Pydantic-моделью")

    if not isinstance(value, dict):
        raise TypeError("Ключ состояния должен сериализоваться в словарь")
    normalized = _normalize_json(value)
    if not all(isinstance(field, str) for field in normalized):
        raise TypeError(
            "Ключ состояния должен сериализоваться в словарь со строковыми ключами"
        )
    _canonical_json(normalized)
    return normalized


def encode_key(key: KeyLike) -> str:
    return _canonical_json({"type": _key_type(key), "value": key_data(key)})


def decode_key(encoded: str, model: type[Any] | None = None) -> Any:
    document = json.loads(encoded)
    if not isinstance(document, dict) or "value" not in document:
        raise ValueError("Сериализованный ключ должен быть объектом с полем 'value'")
    value = document["value"]
    if model is None:
        return value
    if model is dict:
        expected_type = "mapping"
    elif isinstance(model, type) and (issubclass(model, BaseModel) or is_dataclass(model)):
        expected_type = _type_tag(model)
    else:
        raise TypeError(f"Неподдерживаемая модель ключа: {model!r}")
    if document.get("type") != expected_type:
        raise ValueError(
            f"Ключ сериализован как {document.get('type')!r}, ожидался {expected_type!r}"
        )
    if model is dict:
        return value
    if issubclass(model, BaseModel):
        return model.model_validate(value)
    return model(**value)


def storage_key(namespace: str, key: KeyLike) -> str:
    return _storage_key_for_type(namespace, key, _key_type(key))


def _storage_key_for_type(namespace: str, key: KeyLike, key_type: str) -> str:
    encoded = _canonical_json({"type": key_type, "value": key_data(key)}).encode("utf-8")
    token = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
    return f"vkuxt:{KEY_NAMESPACE_VERSION}:{namespace}:{token}"


def _key_type(key: KeyLike) -> str:
    if isinstance(key, BaseModel) or (is_dataclass(key) and not isinstance(key, type)):
        return _type_tag(type(key))
    return "mapping"


def _type_tag(model: type[Any]) -> str:
    custom = getattr(model, "__vkuxt_key_type__", None)
    if custom is not None:
        if not isinstance(custom, str) or not custom:
            raise TypeError("__vkuxt_key_type__ должен быть непустой строкой")
        return custom
    return f"{model.__module__}.{model.__qualname__}"


def _normalize_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON-ключи должны быть строками")
        return {key: _normalize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Значение ключа нельзя сериализовать в JSON: {exc}") from exc
