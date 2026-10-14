"""`python -m src.main.heartbeat`: the scheduler's healthcheck. Exits 0 while
this container's heartbeat is fresh."""

import asyncio
import sys

from redis.asyncio import Redis

from src.core.tasks.heartbeat import heartbeat_is_fresh
from src.main.config import get_settings


async def check(redis_url: str) -> bool:
    redis = Redis.from_url(redis_url)
    try:
        return await heartbeat_is_fresh(redis)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(check(get_settings().redis.tasks_dsn)) else 1)
