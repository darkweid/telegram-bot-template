import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
import redis.exceptions as redis_exc

from src.core.limiter.limiter import RateLimiter
from src.core.limiter.script import RATE_LIMIT_SCRIPT
from src.core.redis.degradation import RedisDegradationReporter
from tests.fakes.redis import InMemoryRedis
from tests.helpers.redis import full_redis

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
    assert await limiter.hit("key", limit=1, window_ms=300) == 0
    assert await limiter.hit("key", limit=1, window_ms=300) > 0
    await asyncio.sleep(0.5)
    assert await limiter.hit("key", limit=1, window_ms=300) == 0


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_the_window_key_expires(backend: Redis | InMemoryRedis) -> None:
    """The window is the key's TTL: a key left without one would count forever."""
    prefix = f"test:{uuid4().hex}"
    limiter = RateLimiter(
        backend, prefix=prefix, reporter=RedisDegradationReporter("RateLimiter")
    )
    await limiter.hit("key", limit=1, window_ms=300)
    assert 0 < await backend.pttl(f"{prefix}:key") <= 300
    await asyncio.sleep(0.5)
    assert await backend.pttl(f"{prefix}:key") == -2


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
    async with full_redis(redis_client) as client:
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
