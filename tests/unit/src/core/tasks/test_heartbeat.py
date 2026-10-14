from datetime import UTC, datetime, timedelta

import pytest

from src.core.tasks import heartbeat
from src.core.tasks.heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TTL_SECONDS,
    LAST_BEAT_KEY,
    HeartbeatScheduleSource,
    heartbeat_is_fresh,
    heartbeat_key,
    last_beat_age,
)
from tests.fakes.redis import InMemoryRedis

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> InMemoryRedis:
    fake = InMemoryRedis()
    monkeypatch.setattr(heartbeat.Redis, "from_url", lambda _url: fake)
    monkeypatch.setattr(heartbeat.socket, "gethostname", lambda: "scheduler-a")
    monkeypatch.setattr(heartbeat, "get_utc_now", lambda: NOW)
    return fake


def test_two_missed_beats_in_a_row_make_the_scheduler_dead() -> None:
    """One late schedule update is tolerated; the spec allows 90 s of silence."""
    assert HEARTBEAT_TTL_SECONDS == 3 * HEARTBEAT_INTERVAL_SECONDS == 90


async def test_each_schedule_update_refreshes_the_heartbeat(
    redis: InMemoryRedis,
) -> None:
    source = HeartbeatScheduleSource("redis://unused")
    await source.startup()
    assert await source.get_schedules() == []
    assert await redis.ttl(heartbeat_key()) == HEARTBEAT_TTL_SECONDS
    assert await heartbeat_is_fresh(redis)
    await source.shutdown()


async def test_the_bot_reads_the_age_of_the_last_beat(redis: InMemoryRedis) -> None:
    """/health and /status cannot know the scheduler's hostname."""
    source = HeartbeatScheduleSource("redis://unused")
    await source.startup()
    await source.get_schedules()
    await source.shutdown()
    later = NOW + timedelta(seconds=40)
    assert await last_beat_age(redis, later) == timedelta(seconds=40)


async def test_no_beat_yet_has_no_age(redis: InMemoryRedis) -> None:
    assert await last_beat_age(redis, NOW) is None
    assert not await redis.exists(LAST_BEAT_KEY)


async def test_a_predecessor_heartbeat_does_not_vouch_for_this_container(
    redis: InMemoryRedis,
) -> None:
    """A deploy replaces the scheduler while the old heartbeat is still fresh."""
    await redis.set(heartbeat_key("scheduler-old"), "alive", ex=60)
    assert not await heartbeat_is_fresh(redis)
