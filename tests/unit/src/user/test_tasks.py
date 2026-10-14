import pytest
import redis.exceptions as redis_exc

from src.core.cache.redis_cache import RedisCache
from src.core.cache.serializer import JsonSerializer
from src.user.cache import user_cache_key
from src.user.schemas import UserProfile
from src.user.tasks import drop_user_snapshot
from tests.factories.users import build_user_snapshot
from tests.fakes.redis import InMemoryRedis


def _cache(redis: InMemoryRedis) -> RedisCache:
    return RedisCache(
        redis, JsonSerializer(), prefix="cache", default_ttl=60, version_ttl=600
    )


async def test_the_task_drops_the_snapshot() -> None:
    cache = _cache(InMemoryRedis())
    await cache.set(user_cache_key(42), build_user_snapshot(id=42))
    await drop_user_snapshot(42, cache)
    assert await cache.get(user_cache_key(42), model=UserProfile) is None


async def test_the_task_fails_while_redis_is_out() -> None:
    """A task that returned would be done for good with the old snapshot
    still cached; failing hands it to the retry."""
    redis = InMemoryRedis()
    cache = _cache(redis)
    await cache.set(user_cache_key(42), build_user_snapshot(id=42))
    redis.fail_next_commands(1)
    with pytest.raises(redis_exc.ConnectionError):
        await drop_user_snapshot(42, cache)
    assert await cache.get(user_cache_key(42), model=UserProfile) is not None
