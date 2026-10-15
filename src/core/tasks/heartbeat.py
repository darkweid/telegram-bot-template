"""Scheduler liveness: a heartbeat the scheduler writes on every schedule update.

Pinging Redis proves only that Redis answers. A scheduler whose loop has
wedged, or that cannot write to the tasks Redis, fires nothing while that ping
stays green. The loop asks every source for its schedules once per update
interval (`--update-interval 30` in compose), so a source that writes on each
ask is a heartbeat of the loop itself.

Two keys: one named after this container, which its healthcheck reads so that
a scheduler replacing another cannot pass on its predecessor's heartbeat, and
`LAST_BEAT_KEY`, which the bot reads for /health and /status.
"""

from datetime import datetime, timedelta
import socket

from redis.asyncio import Redis
from taskiq import ScheduledTask
from taskiq.abc.schedule_source import ScheduleSource

from src.core.utils.datetime_utils import get_utc_now

HEARTBEAT_KEY_PREFIX = "taskiq:scheduler:heartbeat"
LAST_BEAT_KEY = "taskiq:scheduler:last_beat"
# Matches the scheduler's --update-interval in infra/docker-compose.yml.
HEARTBEAT_INTERVAL_SECONDS = 30
# Three intervals: one late update is tolerated, two missed in a row are not.
HEARTBEAT_TTL_SECONDS = 3 * HEARTBEAT_INTERVAL_SECONDS
# Long enough for /status to say how long ago a dead scheduler last beat.
LAST_BEAT_TTL_SECONDS = 7 * 24 * 3600


def heartbeat_key(hostname: str | None = None) -> str:
    return f"{HEARTBEAT_KEY_PREFIX}:{hostname or socket.gethostname()}"


class HeartbeatScheduleSource(ScheduleSource):
    """Contributes no schedules; each ask for them refreshes the heartbeat.

    A failed write raises, which taskiq logs and treats as an empty list, so
    the heartbeat goes stale instead of the scheduler stopping.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis: Redis | None = None

    async def startup(self) -> None:
        self._redis = Redis.from_url(self._redis_url)

    async def shutdown(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def get_schedules(self) -> list[ScheduledTask]:
        if self._redis is None:
            raise RuntimeError("HeartbeatScheduleSource used before startup()")
        await self._redis.set(heartbeat_key(), "alive", ex=HEARTBEAT_TTL_SECONDS)
        await self._redis.set(
            LAST_BEAT_KEY, get_utc_now().isoformat(), ex=LAST_BEAT_TTL_SECONDS
        )
        return []


async def heartbeat_is_fresh(redis: Redis) -> bool:
    return bool(await redis.exists(heartbeat_key()))


async def last_beat_age(redis: Redis, now: datetime) -> timedelta | None:
    """How long ago any scheduler last beat; None when none ever did."""
    raw = await redis.get(LAST_BEAT_KEY)
    if raw is None:
        return None
    value = raw.decode() if isinstance(raw, bytes) else raw
    return now - datetime.fromisoformat(value)
