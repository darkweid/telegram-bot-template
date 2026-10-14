import asyncio
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession

from src.main.config import Config
from src.system import services
from src.system.repositories import SystemRepository
from src.system.services import HealthService


async def _hang(*_: object) -> None:
    await asyncio.sleep(3600)


def _service(
    settings: Config,
    *,
    postgres: Callable[..., Awaitable[None]] | BaseException | None = None,
    redis: Callable[..., Awaitable[None]] | BaseException | None = None,
    memory: dict[str, int] | BaseException | None = None,
) -> HealthService:
    """`postgres`/`redis`: None answers, an exception is raised, a coroutine
    function runs instead (e.g. `_hang`). `memory`: what INFO memory returns."""
    repository = AsyncMock(spec=SystemRepository)
    repository.ping.side_effect = postgres
    client = AsyncMock(spec=Redis)
    # redis-py's command methods are plain functions returning awaitables, so a
    # spec'd AsyncMock makes them MagicMocks that cannot be awaited.
    client.ping = AsyncMock(return_value=True, side_effect=redis)
    client.info = AsyncMock(
        return_value=memory or {"used_memory": 10, "maxmemory": 0},
        side_effect=memory if isinstance(memory, BaseException) else None,
    )
    return HealthService(
        repository, AsyncMock(spec=AsyncSession), client, settings.app, settings.bot
    )


async def test_ready_when_both_answer(settings: Config) -> None:
    readiness = await _service(settings).check_readiness()
    assert (readiness.postgres, readiness.redis, readiness.ok) == (True, True, True)


async def test_not_ready_without_postgres(settings: Config) -> None:
    service = _service(settings, postgres=OSError("connection refused"))
    readiness = await service.check_readiness()
    assert (readiness.postgres, readiness.redis, readiness.ok) == (False, True, False)


async def test_not_ready_without_redis(settings: Config) -> None:
    service = _service(settings, redis=RedisConnectionError("refused"))
    readiness = await service.check_readiness()
    assert (readiness.postgres, readiness.redis, readiness.ok) == (True, False, False)


async def test_a_hanging_dependency_fails_within_the_budget(
    settings: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "PROBE_TIMEOUT_SECONDS", 0.05)
    async with asyncio.timeout(1):
        readiness = await _service(
            settings, postgres=_hang, redis=_hang
        ).check_readiness()
    assert (readiness.postgres, readiness.redis) == (False, False)


async def test_report_names_version_and_mode(settings: Config) -> None:
    report = await _service(settings).get_report()
    assert report.status == "ok"
    assert report.version == settings.app.VERSION
    assert report.mode == settings.bot.BOT_MODE
    assert report.redis_memory_used_ratio is None  # no maxmemory cap


async def test_report_degrades_when_redis_is_nearly_full(settings: Config) -> None:
    service = _service(settings, memory={"used_memory": 95, "maxmemory": 100})
    report = await service.get_report()
    assert (report.status, report.redis_memory_used_ratio) == ("degraded", 0.95)


async def test_report_degrades_and_still_answers_when_postgres_is_down(
    settings: Config,
) -> None:
    report = await _service(settings, postgres=OSError("down")).get_report()
    assert (report.status, report.postgres, report.redis) == ("degraded", False, True)


async def test_unreadable_redis_memory_is_not_an_outage(settings: Config) -> None:
    report = await _service(settings, memory=RedisConnectionError("reset")).get_report()
    assert (report.status, report.redis_memory_used_ratio) == ("ok", None)


async def test_hanging_redis_memory_read_is_not_an_outage(
    settings: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "PROBE_TIMEOUT_SECONDS", 0.05)
    service = _service(settings)
    service.redis.info = AsyncMock(side_effect=_hang)
    async with asyncio.timeout(1):
        report = await service.get_report()
    assert (report.status, report.redis_memory_used_ratio) == ("ok", None)
