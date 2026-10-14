import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
import redis.exceptions as redis_exc

from src.core.limiter.limiter import RateLimiter
from src.core.limiter.script import RATE_LIMIT_SCRIPT
from src.core.redis.degradation import RedisDegradationReporter
from tests.fakes.redis import InMemoryRedis

pytestmark = pytest.mark.asyncio(loop_scope="session")

BACKENDS = ["real", "fake"]


@pytest_asyncio.fixture(loop_scope="session")
async def backend(
    request: pytest.FixtureRequest, redis_client: Redis
) -> Redis | InMemoryRedis:
    return redis_client if request.param == "real" else InMemoryRedis()


async def _limiter(client: Redis | InMemoryRedis) -> RateLimiter:
    return RateLimiter(
        client,
        prefix=f"test:{uuid4().hex}",
        reporter=RedisDegradationReporter("RateLimiter"),
    )


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_the_fake_answers_like_the_real_script(
    backend: Redis | InMemoryRedis,
) -> None:
    """Unit tests trust the fake's emulation; this keeps it honest."""
    limiter = await _limiter(backend)
    answers = [await limiter.hit("key", limit=2, window_ms=60_000) for _ in range(3)]
    assert answers[:2] == [0, 0]
    assert 0 < answers[2] <= 60_000


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_a_new_window_opens_after_the_key_expires(
    backend: Redis | InMemoryRedis,
) -> None:
    """A limiter that never reopens its window would lock a user out for good;
    both the script and its emulation must start over once the key is gone."""
    limiter = await _limiter(backend)
    assert await limiter.hit("key", limit=1, window_ms=30) == 0
    assert await limiter.hit("key", limit=1, window_ms=30) > 0
    await asyncio.sleep(0.1)
    assert await limiter.hit("key", limit=1, window_ms=30) == 0


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_the_window_key_expires(backend: Redis | InMemoryRedis) -> None:
    """The window is the key's TTL: a key left without one would count forever."""
    prefix = f"test:{uuid4().hex}"
    limiter = RateLimiter(
        backend, prefix=prefix, reporter=RedisDegradationReporter("RateLimiter")
    )
    await limiter.hit("key", limit=1, window_ms=30)
    assert 0 < await backend.pttl(f"{prefix}:key") <= 30
    await asyncio.sleep(0.1)
    assert await backend.pttl(f"{prefix}:key") == -2


@asynccontextmanager
async def _full(client: Redis) -> AsyncIterator[Redis]:
    """The throwaway test instance with no room left and noeviction."""
    maxmemory = await client.config_get("maxmemory")
    policy = await client.config_get("maxmemory-policy")
    await client.config_set("maxmemory-policy", "noeviction")
    await client.config_set("maxmemory", "1")
    try:
        yield client
    finally:
        await client.config_set("maxmemory", maxmemory["maxmemory"])
        await client.config_set("maxmemory-policy", policy["maxmemory-policy"])


async def _outcome(client: Redis | InMemoryRedis, sha: str, key: str) -> str:
    try:
        answer = await client.evalsha(sha, 1, key, "2", "60000")
    except redis_exc.OutOfMemoryError:
        return "oom"
    return "admitted" if answer == 0 else "refused"


async def test_the_fake_refuses_what_a_full_redis_refuses(
    redis_client: Redis,
) -> None:
    """Recovery is reported on an admitted call only; that is sound only while
    the fake refuses the same calls real Redis does: INCR and SET, not reads."""
    fake = InMemoryRedis()
    real_prefix = f"test:{uuid4().hex}"
    keys = {
        "within_limit": f"{real_prefix}:within",
        "at_limit": f"{real_prefix}:at",
        "new_key": f"{real_prefix}:new",
    }
    shas = {
        "real": await redis_client.script_load(RATE_LIMIT_SCRIPT),
        "fake": await fake.script_load(RATE_LIMIT_SCRIPT),
    }
    for client, sha in ((redis_client, shas["real"]), (fake, shas["fake"])):
        assert await _outcome(client, sha, keys["within_limit"]) == "admitted"
        assert await _outcome(client, sha, keys["at_limit"]) == "admitted"
        assert await _outcome(client, sha, keys["at_limit"]) == "admitted"

    outcomes: dict[str, dict[str, str]] = {}
    fake.out_of_memory = True
    outcomes["fake"] = {
        name: await _outcome(fake, shas["fake"], key) for name, key in keys.items()
    }
    async with _full(redis_client) as client:
        outcomes["real"] = {
            name: await _outcome(client, shas["real"], key)
            for name, key in keys.items()
        }
    assert (
        outcomes["real"]
        == outcomes["fake"]
        == {
            "within_limit": "oom",
            "at_limit": "refused",
            "new_key": "oom",
        }
    )
