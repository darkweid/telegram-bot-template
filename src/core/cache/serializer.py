from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from typing import Any, TypeVar, cast, overload
from uuid import UUID

from pydantic import BaseModel, TypeAdapter

from src.core.cache.interface import JsonValue

T = TypeVar("T")

# functools.lru_cache's stub types __call__ as accepting only Hashable, which
# type[Any] does not satisfy under strict mypy - a plain dict cache sidesteps that.
_adapters: dict[type[Any], TypeAdapter[Any]] = {}


def _adapter(model: type[T]) -> TypeAdapter[T]:
    if model not in _adapters:
        _adapters[model] = TypeAdapter(model)
    # The dict loses the pairing of each model with its adapter.
    return cast(TypeAdapter[T], _adapters[model])


def _encode_unknown(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, BaseModel):
        # by_alias: a model with aliased fields must round-trip under the keys
        # it is validated against, or extra="forbid" rejects the payload on read.
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    raise TypeError(f"Cache cannot serialize {type(value).__name__}")


class JsonSerializer:
    """JSON codec for cache values with optional typed decoding."""

    def dumps(self, value: Any) -> str:
        return json.dumps(value, default=_encode_unknown)

    @overload
    def loads(self, raw: str, model: type[T]) -> T: ...

    @overload
    def loads(self, raw: str, model: None = None) -> JsonValue: ...

    def loads(self, raw: str, model: type[T] | None = None) -> T | JsonValue:
        # Without `model`, non-string types (Decimal, UUID, datetime) come back
        # as plain strings: typed decoding is required wherever they are expected.
        payload: JsonValue = json.loads(raw)
        if model is None:
            return payload
        return _adapter(model).validate_python(payload)
