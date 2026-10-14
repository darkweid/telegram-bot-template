from unittest.mock import MagicMock

import pytest
import redis.exceptions as redis_exc

from src.core.limiter.limiter import RateLimiter
from src.core.redis.degradation import RedisDegradationReporter
from tests.fakes.redis import InMemoryRedis


@pytest.fixture
def sentry_capture(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr("src.core.redis.degradation.sentry_sdk.capture_message", mock)
    return mock


@pytest.fixture
def redis() -> InMemoryRedis:
    return InMemoryRedis()


def _limiter(redis: InMemoryRedis, **kwargs: object) -> RateLimiter:
    return RateLimiter(
        redis,
        prefix="limiter",
        reporter=RedisDegradationReporter("RateLimiter"),
        **kwargs,
    )


async def test_calls_within_the_limit_are_admitted(redis: InMemoryRedis) -> None:
    limiter = _limiter(redis)
    assert [await limiter.hit("a", limit=3, window_ms=60_000) for _ in range(3)] == [
        0,
        0,
        0,
    ]


async def test_the_call_past_the_limit_learns_how_long_to_wait(
    redis: InMemoryRedis,
) -> None:
    limiter = _limiter(redis)
    for _ in range(2):
        await limiter.hit("a", limit=2, window_ms=60_000)
    wait_ms = await limiter.hit("a", limit=2, window_ms=60_000)
    assert 0 < wait_ms <= 60_000


async def test_keys_have_separate_budgets_under_the_prefix(
    redis: InMemoryRedis,
) -> None:
    limiter = _limiter(redis)
    await limiter.hit("a", limit=1, window_ms=60_000)
    assert await limiter.hit("b", limit=1, window_ms=60_000) == 0
    assert redis.evalsha_keys == ["limiter:a", "limiter:b"]


async def test_a_flushed_script_cache_is_reloaded(redis: InMemoryRedis) -> None:
    """SCRIPT FLUSH or a Redis restart forgets the script; the limiter must
    reload it instead of failing every update until the bot restarts."""
    limiter = _limiter(redis)
    await limiter.hit("a", limit=5, window_ms=60_000)
    redis._scripts.clear()
    assert await limiter.hit("a", limit=5, window_ms=60_000) == 0


async def test_an_unreachable_redis_falls_back_to_counting_in_memory(
    redis: InMemoryRedis, sentry_capture: MagicMock
) -> None:
    """Throttling must keep working through a Redis outage: without it a
    flood that took Redis down would then run unthrottled."""
    limiter = _limiter(redis)
    redis.fail_next_commands(10)
    results = [await limiter.hit("a", limit=2, window_ms=60_000) for _ in range(3)]
    assert results[:2] == [0, 0]
    assert results[2] > 0
    assert sentry_capture.call_count == 1


async def test_an_admitted_call_on_a_full_redis_falls_back_and_degrades(
    redis: InMemoryRedis, sentry_capture: MagicMock
) -> None:
    """A full Redis refuses INCR as it refuses SET: treating the call on an
    existing key as success would report recovery in the middle of an outage."""
    limiter = _limiter(redis)
    await limiter.hit("a", limit=5, window_ms=60_000)
    redis.out_of_memory = True
    assert await limiter.hit("a", limit=5, window_ms=60_000) == 0
    assert sentry_capture.call_count == 1
    assert "degraded" in sentry_capture.call_args.args[0]


async def test_a_refusal_on_a_full_redis_is_still_answered_by_redis(
    redis: InMemoryRedis, sentry_capture: MagicMock
) -> None:
    limiter = _limiter(redis)
    await limiter.hit("a", limit=1, window_ms=60_000)
    redis.out_of_memory = True
    assert await limiter.hit("a", limit=1, window_ms=60_000) > 0
    assert sentry_capture.call_count == 0


async def test_a_broken_script_is_raised_not_hidden(redis: InMemoryRedis) -> None:
    """A ResponseError is a bug in the script or its arguments; falling back
    would hide it behind a fake outage."""
    limiter = _limiter(redis)
    redis.fail_next_commands(error=redis_exc.ResponseError("ERR syntax"))
    with pytest.raises(redis_exc.ResponseError):
        await limiter.hit("a", limit=1, window_ms=60_000)


async def test_only_an_admitted_call_closes_the_incident(
    redis: InMemoryRedis, sentry_capture: MagicMock
) -> None:
    clock_ms = [0]
    reporter = RedisDegradationReporter(
        "RateLimiter", recovery_quiet_ms=0, clock=lambda: clock_ms[0]
    )
    limiter = RateLimiter(redis, prefix="limiter", reporter=reporter)
    await limiter.hit("a", limit=1, window_ms=60_000)
    redis.fail_next_commands()
    await limiter.hit("b", limit=1, window_ms=60_000)  # fallback, degraded
    clock_ms[0] = 1
    await limiter.hit("a", limit=1, window_ms=60_000)  # refused by Redis
    assert sentry_capture.call_count == 1
    await limiter.hit("c", limit=1, window_ms=60_000)  # admitted by Redis
    assert sentry_capture.call_count == 2


async def test_the_fallback_stays_bounded(
    redis: InMemoryRedis, sentry_capture: MagicMock
) -> None:
    """A flood of distinct keys during an outage must not grow memory without
    limit; the oldest window goes first."""
    limiter = _limiter(redis, fallback_max_entries=2)
    redis.fail_next_commands(3)
    for key in ("a", "b", "c"):
        await limiter.hit(key, limit=1, window_ms=60_000)
    assert len(limiter._fallback_windows) == 2
    assert "a" not in limiter._fallback_windows


@pytest.mark.parametrize(("limit", "window_ms"), [(0, 1000), (1, 0)])
async def test_a_meaningless_limit_is_rejected(
    redis: InMemoryRedis, limit: int, window_ms: int
) -> None:
    with pytest.raises(ValueError):
        await _limiter(redis).hit("a", limit=limit, window_ms=window_ms)
