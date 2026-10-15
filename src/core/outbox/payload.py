import json
from typing import Any

# PostgreSQL stores a JSONB number as `numeric` and prints it back without an
# exponent. From 1e16 up a float's repr switches to one (1e+16), and JSONB
# returns it as 10000000000000000, which decodes to an int. Below the limit a
# float is printed in positional form and decodes to the same float.
JSONB_FLOAT_LIMIT = 1e16


def _changed_by_jsonb(value: Any) -> bool:
    """True when JSONB itself would change or refuse a value Python's json
    round-trip keeps: a float at or past JSONB_FLOAT_LIMIT, or a NUL in a
    string or key (JSONB has no NUL and fails the INSERT)."""
    if type(value) is float:
        return abs(value) >= JSONB_FLOAT_LIMIT
    if type(value) is str:
        return "\x00" in value
    if type(value) is dict:
        return any(
            _changed_by_jsonb(key) or _changed_by_jsonb(item)
            for key, item in value.items()
        )
    if type(value) is list:
        return any(_changed_by_jsonb(item) for item in value)
    return False


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
    original while a sweeper republish sends the coerced form. So are the
    values JSONB alone changes or rejects: floats from JSONB_FLOAT_LIMIT up,
    and strings or keys holding a NUL.
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
    if _changed_by_jsonb(payload):
        raise TypeError(
            f"Arguments for task '{task_name}' must survive a JSONB round-trip "
            f"unchanged: no float of magnitude {JSONB_FLOAT_LIMIT:g} or more "
            "(it comes back as an int) and no NUL in strings or keys"
        )
