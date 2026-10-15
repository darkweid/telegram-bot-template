import pytest
import redis.exceptions as redis_exc

from tests.fakes.redis import InMemoryRedis


async def test_a_failed_setex_costs_one_failure() -> None:
    """Tests count failures per client command: a setex that used up two
    would let the next real command through one failure early."""
    redis = InMemoryRedis()
    redis.fail_next_commands(2)
    for _ in range(2):
        with pytest.raises(redis_exc.ConnectionError):
            await redis.setex("key", 60, "value")
    assert await redis.setex("key", 60, "value") is True
    assert await redis.get("key") == "value"
