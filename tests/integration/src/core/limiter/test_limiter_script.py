from uuid import uuid4

import pytest
from redis.asyncio import Redis

from src.core.limiter.limiter import RateLimiter
from src.core.redis.degradation import RedisDegradationReporter
from tests.fakes.redis import InMemoryRedis

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _answers(client: Redis | InMemoryRedis, prefix: str) -> list[int]:
    limiter = RateLimiter(
        client,
        prefix=prefix,
        reporter=RedisDegradationReporter("RateLimiter"),
    )
    return [await limiter.hit("key", limit=2, window_ms=60_000) for _ in range(3)]


async def test_the_fake_answers_like_the_real_script(redis_client: Redis) -> None:
    """Unit tests trust the fake's emulation; this keeps it honest."""
    real = await _answers(redis_client, f"test:{uuid4().hex}")
    fake = await _answers(InMemoryRedis(), "test")
    assert real[:2] == fake[:2] == [0, 0]
    assert 0 < real[2] <= 60_000 and 0 < fake[2] <= 60_000


async def test_the_window_expires_with_the_key(redis_client: Redis) -> None:
    prefix = f"test:{uuid4().hex}"
    limiter = RateLimiter(
        redis_client, prefix=prefix, reporter=RedisDegradationReporter("RateLimiter")
    )
    await limiter.hit("key", limit=1, window_ms=60_000)
    assert 0 < await redis_client.pttl(f"{prefix}:key") <= 60_000
