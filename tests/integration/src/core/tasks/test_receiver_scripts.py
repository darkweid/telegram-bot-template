from uuid import uuid4

import pytest
from redis.asyncio import Redis

from src.core.tasks.receiver import RELEASE_CLAIM_SCRIPT, RENEW_CLAIM_SCRIPT
from tests.fakes.redis import InMemoryRedis

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _answers(client: Redis | InMemoryRedis, key: str) -> list[object]:
    await client.set(key, "mine", ex=60)
    return [
        await client.eval(RENEW_CLAIM_SCRIPT, 1, key, "theirs", "120"),
        await client.ttl(key) <= 60,
        await client.eval(RENEW_CLAIM_SCRIPT, 1, key, "mine", "120"),
        await client.ttl(key) > 60,
        await client.eval(RELEASE_CLAIM_SCRIPT, 1, key, "theirs"),
        await client.exists(key),
        await client.eval(RELEASE_CLAIM_SCRIPT, 1, key, "mine"),
        await client.exists(key),
    ]


async def test_the_fake_claim_scripts_answer_like_redis(redis_client: Redis) -> None:
    """Unit tests of the receiver trust the fake; this is why they may."""
    key = f"test:claim:{uuid4().hex}"
    try:
        real = await _answers(redis_client, key)
    finally:
        await redis_client.delete(key)
    assert real == await _answers(InMemoryRedis(), "claim")
    assert real == [0, True, 1, True, 0, 1, 1, 0]
