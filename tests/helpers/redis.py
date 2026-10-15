from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis


@asynccontextmanager
async def full_redis(client: Redis) -> AsyncIterator[Redis]:
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
