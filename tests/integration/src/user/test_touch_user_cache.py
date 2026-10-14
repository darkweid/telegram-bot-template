from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis

from src.core.cache.redis_cache import RedisCache
from src.core.cache.serializer import JsonSerializer
from src.core.redis.degradation import RedisDegradationReporter
from src.user.cache import USERS_CACHE_NAMESPACE, user_cache_key
from src.user.schemas import UserProfile

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_snapshot_survives_the_redis_round_trip(redis_client: Redis) -> None:
    """A snapshot that came back different (naive datetime, missing field)
    would make every cached update compare wrong in needs_write."""
    cache = RedisCache(
        redis_client,
        JsonSerializer(),
        prefix=f"test-{id(redis_client)}",
        default_ttl=60,
        version_ttl=600,
        reporter=RedisDegradationReporter("Cache"),
    )
    snapshot = UserProfile(
        id=42,
        first_name="Ann",
        username=None,
        last_name=None,
        language_code="en",
        locale="ru",
        is_reachable=True,
        is_banned=False,
        last_seen_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )
    await cache.set(user_cache_key(42), snapshot)
    try:
        assert await cache.get(user_cache_key(42), model=UserProfile) == snapshot
    finally:
        await cache.invalidate(USERS_CACHE_NAMESPACE)
