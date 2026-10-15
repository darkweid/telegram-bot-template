import pytest

from src.core.tasks.heartbeat import HeartbeatScheduleSource
from src.main import heartbeat as healthcheck
from tests.fakes.redis import InMemoryRedis


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> InMemoryRedis:
    fake = InMemoryRedis()
    monkeypatch.setattr(healthcheck.Redis, "from_url", lambda _url: fake)
    return fake


async def test_the_healthcheck_passes_once_the_loop_has_beaten(
    redis: InMemoryRedis,
) -> None:
    """Docker restarts the scheduler on a failed check; a fresh one must pass."""
    assert not await healthcheck.check("redis://unused")
    source = HeartbeatScheduleSource("redis://unused")
    await source.startup()
    await source.get_schedules()
    await source.shutdown()
    assert await healthcheck.check("redis://unused")
