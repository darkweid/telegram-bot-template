from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from src.core.cache.interface import CacheKey
from src.core.cache.redis_cache import RedisCache
from src.core.cache.serializer import JsonSerializer
from src.core.redis.degradation import RedisDegradationReporter
from tests.fakes.redis import InMemoryRedis
from tests.helpers.redis import full_redis

pytestmark = pytest.mark.asyncio(loop_scope="session")

KEY = CacheKey("users", "1", tags=("t",))
SIBLING = CacheKey("users", "2", tags=("t",))


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, message: str) -> None:
        self.messages.append(message)


@pytest_asyncio.fixture(loop_scope="session")
async def backend(
    request: pytest.FixtureRequest, redis_client: Redis
) -> Redis | InMemoryRedis:
    return redis_client if request.param == "real" else InMemoryRedis()


def _cache(
    client: Redis | InMemoryRedis,
    prefix: str,
    *,
    version_ttl: int = 600,
    notifier: RecordingNotifier | None = None,
) -> RedisCache:
    return RedisCache(
        client,
        JsonSerializer(),
        prefix=prefix,
        default_ttl=60,
        version_ttl=version_ttl,
        reporter=RedisDegradationReporter("Cache", cooldown_ms=0, notifier=notifier),
    )


def _prefix() -> str:
    return f"test:{uuid4().hex}"


async def _ttls(client: Redis | InMemoryRedis, prefix: str) -> dict[str, int]:
    """TTL in seconds of every key under the prefix, by key."""
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await client.scan(cursor, match=f"{prefix}*", count=100)
        keys.extend(batch)
        if cursor == 0:
            break
    return {key: await client.ttl(key) for key in keys}


@pytest.mark.parametrize("backend", ["real", "fake"], indirect=True)
async def test_set_get_and_both_kinds_of_invalidation(
    backend: Redis | InMemoryRedis,
) -> None:
    """The unit suite trusts the fake's emulation of the four cache scripts."""
    cache = _cache(backend, _prefix())
    await cache.set(KEY, {"a": 1})
    assert await cache.get(KEY) == {"a": 1}
    await cache.invalidate_tags("t")
    assert await cache.get(KEY) is None
    await cache.set(KEY, {"a": 2})
    await cache.invalidate("users")
    assert await cache.get(KEY) is None


@pytest.mark.parametrize("backend", ["real", "fake"], indirect=True)
async def test_delete_removes_only_its_own_entry(
    backend: Redis | InMemoryRedis,
) -> None:
    cache = _cache(backend, _prefix())
    await cache.set(KEY, {"a": 1})
    await cache.set(SIBLING, {"a": 2})
    await cache.delete(KEY)
    assert await cache.get(KEY) is None
    assert await cache.get(SIBLING) == {"a": 2}


@pytest.mark.parametrize("backend", ["real", "fake"], indirect=True)
async def test_a_raising_delete_removes_only_its_own_entry(
    backend: Redis | InMemoryRedis,
) -> None:
    """The snapshot task deletes through this path; the fake must agree."""
    cache = _cache(backend, _prefix())
    await cache.set(KEY, {"a": 1})
    await cache.set(SIBLING, {"a": 2})
    await cache.delete_or_raise(KEY)
    assert await cache.get(KEY) is None
    assert await cache.get(SIBLING) == {"a": 2}


@pytest.mark.parametrize("backend", ["real", "fake"], indirect=True)
async def test_a_retired_generation_stays_unreachable_after_a_sibling_write(
    backend: Redis | InMemoryRedis,
) -> None:
    """A write that recreates a deleted counter must start a new generation, or
    a value stored under the retired one would come back to life."""
    cache = _cache(backend, _prefix())
    await cache.set(KEY, {"a": 1})
    await cache.invalidate_tags("t")
    await cache.set(SIBLING, {"a": 2})
    assert await cache.get(KEY) is None
    assert await cache.get(SIBLING) == {"a": 2}


@pytest.mark.parametrize("backend", ["real", "fake"], indirect=True)
async def test_counters_are_refreshed_and_values_expire(
    backend: Redis | InMemoryRedis,
) -> None:
    """A namespace in use must stay reachable: each write gives its counters the
    full version TTL back, whichever TTL the first write gave them."""
    prefix = _prefix()
    await _cache(backend, prefix, version_ttl=100).set(KEY, {"a": 1}, ttl=30)
    await _cache(backend, prefix, version_ttl=600).set(SIBLING, {"a": 2}, ttl=40)
    ttls = await _ttls(backend, prefix)
    counters = {k: v for k, v in ttls.items() if "-ver:" in k or "-tag:" in k}
    values = {k: v for k, v in ttls.items() if k not in counters}
    assert len(counters) == 2 and len(values) == 2
    assert all(100 < ttl <= 600 for ttl in counters.values())
    assert sorted(round(ttl, -1) for ttl in values.values()) == [30, 40]


@asynccontextmanager
async def _fake_full(client: InMemoryRedis) -> AsyncIterator[InMemoryRedis]:
    client.out_of_memory = True
    try:
        yield client
    finally:
        client.out_of_memory = False


async def _oom_outcomes(
    cache: RedisCache, notifier: RecordingNotifier
) -> dict[str, tuple[object, int]]:
    """Each step's visible result with the count of degradation messages so far."""
    outcomes: dict[str, tuple[object, int]] = {}
    outcomes["get"] = (await cache.get(KEY), len(notifier.messages))
    await cache.set(KEY, {"a": 3})
    outcomes["set"] = (await cache.get(KEY), len(notifier.messages))
    await cache.delete(KEY)
    outcomes["delete"] = (await cache.get(KEY), len(notifier.messages))
    await cache.invalidate("users")
    outcomes["invalidate"] = (await cache.get(SIBLING), len(notifier.messages))
    return outcomes


async def test_a_full_redis_refuses_only_the_cache_write(redis_client: Redis) -> None:
    """The unit suite relies on the fake refusing writes only: reads, DEL and
    invalidation keep working on a full instance, so none of them may report
    recovery or degradation."""
    results = []
    for client in (redis_client, InMemoryRedis()):
        notifier = RecordingNotifier()
        cache = _cache(client, _prefix(), notifier=notifier)
        await cache.set(KEY, {"a": 1})
        await cache.set(SIBLING, {"a": 2})
        full = full_redis(client) if isinstance(client, Redis) else _fake_full(client)
        async with full:
            results.append(await _oom_outcomes(cache, notifier))
    assert (
        results[0]
        == results[1]
        == {
            "get": ({"a": 1}, 0),
            "set": ({"a": 1}, 1),
            "delete": (None, 1),
            "invalidate": (None, 1),
        }
    )
