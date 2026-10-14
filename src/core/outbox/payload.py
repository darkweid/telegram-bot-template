import json
from typing import Any


def _survives_json_round_trip(original: Any, decoded: Any) -> bool:
    """
    Type-strict deep equality between a value and its JSON round-trip.

    Plain `==` is not enough: a `StrEnum` compares equal to its string, an
    `IntEnum` to its int, and dict subclasses to plain dicts - yet the inline
    publish would send the original object while a sweeper republish sends the
    decoded primitive from the JSONB row.
    """
    if type(original) is not type(decoded):
        return False
    if type(original) is dict:
        if len(original) != len(decoded):
            return False
        return all(
            type(original_key) is type(decoded_key)
            and original_key == decoded_key
            and _survives_json_round_trip(original_value, decoded_value)
            for (original_key, original_value), (decoded_key, decoded_value) in zip(
                original.items(), decoded.items(), strict=True
            )
        )
    if type(original) is list:
        if len(original) != len(decoded):
            return False
        return all(
            _survives_json_round_trip(original_item, decoded_item)
            for original_item, decoded_item in zip(original, decoded, strict=True)
        )
    return bool(original == decoded)


def ensure_json_payload(
    task_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    """Refuse task arguments a JSONB outbox row would change.

    A non-JSON value (UUID, datetime, NaN, a model) would otherwise fail deep
    inside the INSERT flush. Values JSON merely coerces (enums, int dict keys,
    nested tuples) are refused too: the after-commit publish would send the
    original while a sweeper republish sends the coerced form.
    """
    payload = {"args": list(args), "kwargs": kwargs}
    try:
        encoded = json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"Arguments for task '{task_name}' must be JSON-serializable "
            f"(they are stored in a JSONB outbox row): {exc}"
        ) from exc
    if not _survives_json_round_trip(payload, json.loads(encoded)):
        raise TypeError(
            f"Arguments for task '{task_name}' must survive a JSON round-trip "
            "unchanged: pass ids and plain values, not enums, int dict keys "
            "or nested tuples"
        )
