from uuid import uuid4

import pytest
from redis.asyncio import Redis

from src.core.cache.interface import Cache, CacheKey
from src.core.cache.redis_cache import RedisCache
from src.core.cache.serializer import JsonSerializer
from tests.fakes.redis import InMemoryRedis

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _cache(client: Redis | InMemoryRedis) -> Cache:
    return RedisCache(
        client,
        JsonSerializer(),
        prefix=f"test:{uuid4().hex}",
        default_ttl=60,
        version_ttl=600,
    )


@pytest.mark.parametrize("backend", ["real", "fake"])
async def test_set_get_invalidate_behaves_the_same_on_both_backends(
    redis_client: Redis, backend: str
) -> None:
    """The unit suite trusts the fake's emulation of the four cache scripts."""
    cache = _cache(redis_client if backend == "real" else InMemoryRedis())
    key = CacheKey("users", "1", tags=("t",))
    await cache.set(key, {"a": 1})
    assert await cache.get(key) == {"a": 1}
    await cache.invalidate_tags("t")
    assert await cache.get(key) is None
    await cache.set(key, {"a": 2})
    await cache.invalidate("users")
    assert await cache.get(key) is None
