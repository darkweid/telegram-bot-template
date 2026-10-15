from enum import StrEnum
from uuid import uuid4

import pytest

from src.core.outbox.payload import ensure_json_payload


class Color(StrEnum):
    RED = "red"


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        ((1, "a", None, True, 1.5), {}),
        ((), {"ids": [1, 2], "nested": {"a": [{"b": None}]}}),
        ((9999999999999998.0, -9999999999999998.0, 1e-07, 0.1 + 0.2), {}),
    ],
)
def test_plain_json_values_are_accepted(
    args: tuple[object, ...], kwargs: dict[str, object]
) -> None:
    ensure_json_payload("task", args, kwargs)


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        ((uuid4(),), {}),
        ((float("nan"),), {}),
        ((), {"color": Color.RED}),
        ((), {"by_id": {1: "a"}}),
        ((), {"by_color": {Color.RED: "a"}}),
        (((1, 2),), {}),
        ((), {"pair": [(1, 2)]}),
        ((1e16,), {}),
        ((), {"amounts": [-2.5e20]}),
        (("a\x00b",), {}),
        ((), {"names": {"a\x00": "b"}}),
    ],
)
def test_values_json_would_change_are_refused(
    args: tuple[object, ...], kwargs: dict[str, object]
) -> None:
    """The after-commit publish sends the original values while a sweeper
    republish sends what the JSONB row holds; one task must not get two
    different payloads depending on who delivered it."""
    with pytest.raises(TypeError, match="task"):
        ensure_json_payload("task", args, kwargs)
