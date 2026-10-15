import pytest
import redis.exceptions as redis_exc

from src.core.redis.errors import FAIL_OPEN_ERRORS


@pytest.mark.parametrize(
    "error",
    [
        redis_exc.ConnectionError("down"),
        redis_exc.TimeoutError("slow"),
        redis_exc.OutOfMemoryError("OOM"),
    ],
)
def test_an_outage_is_failed_open_on(error: Exception) -> None:
    assert isinstance(error, FAIL_OPEN_ERRORS)


def test_a_plain_response_error_is_not_an_outage() -> None:
    """A malformed argument or a broken script is a programmer error: failing
    open on it would hide the bug behind a fake outage and spend the
    degradation reporter's cooldown on it."""
    assert not isinstance(redis_exc.ResponseError("ERR syntax"), FAIL_OPEN_ERRORS)
