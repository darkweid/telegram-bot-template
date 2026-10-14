import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError, ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.outbox.repositories import OutboxRepository
from src.core.tasks.broker import TasksRedis
from src.core.tasks.heartbeat import HEARTBEAT_TTL_SECONDS
from src.main.config import Config
from src.system import services
from src.system.repositories import SystemRepository
from src.system.services import HealthService

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "get_utc_now", lambda: NOW)


async def _hang(*_: object) -> None:
    await asyncio.sleep(3600)


def _service(
    settings: Config,
    *,
    postgres: Callable[..., Awaitable[None]] | BaseException | None = None,
    redis: Callable[..., Awaitable[None]] | BaseException | None = None,
    fsm: Callable[..., Awaitable[None]] | BaseException | None = None,
    tasks: Callable[..., Awaitable[None]] | BaseException | None = None,
    memory: dict[str, int] | BaseException | None = None,
    beat_age: timedelta | None = timedelta(seconds=10),
    pending: tuple[int, timedelta | None] = (0, None),
) -> HealthService:
    """`postgres`/`redis`/`fsm`/`tasks`: None answers, an exception is raised,
    a coroutine function runs instead (e.g. `_hang`). `memory`: what INFO
    memory returns. `beat_age`: how long ago the scheduler beat, None for
    never. `pending`: the outbox backlog, count and oldest age."""
    repository = AsyncMock(spec=SystemRepository)
    repository.ping.side_effect = postgres
    outbox = AsyncMock(spec=OutboxRepository)
    outbox.count_pending.return_value = pending[0]
    outbox.oldest_pending_age.return_value = pending[1]
    client = AsyncMock(spec=Redis)
    # redis-py's command methods are plain functions returning awaitables, so a
    # spec'd AsyncMock makes them MagicMocks that cannot be awaited.
    client.ping = AsyncMock(return_value=True, side_effect=redis)
    client.info = AsyncMock(
        return_value=memory or {"used_memory": 10, "maxmemory": 0},
        side_effect=memory if isinstance(memory, BaseException) else None,
    )
    fsm_client = AsyncMock(spec=Redis)
    fsm_client.ping = AsyncMock(return_value=True, side_effect=fsm)
    tasks_client = AsyncMock(spec=Redis)
    tasks_client.ping = AsyncMock(return_value=True, side_effect=tasks)
    tasks_client.xlen = AsyncMock(return_value=3)
    tasks_client.get = AsyncMock(
        return_value=None if beat_age is None else (NOW - beat_age).isoformat().encode()
    )
    return HealthService(
        repository,
        outbox,
        AsyncMock(spec=AsyncSession),
        client,
        RedisStorage(redis=fsm_client),
        TasksRedis(tasks_client),
        settings.app,
        settings.bot,
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


async def test_not_ready_when_the_fsm_database_refuses(settings: Config) -> None:
    """The cache client answering says nothing about the FSM's own database,
    and every update reads or locks there."""
    service = _service(settings, fsm=ResponseError("DB index is out of range"))
    readiness = await service.check_readiness()
    assert (readiness.postgres, readiness.redis, readiness.ok) == (True, False, False)


async def test_not_ready_when_the_task_database_refuses(settings: Config) -> None:
    """The bot sends every task there after commit; a refused index fails each
    send while the cache client still answers."""
    service = _service(settings, tasks=ResponseError("DB index is out of range"))
    readiness = await service.check_readiness()
    assert (readiness.postgres, readiness.redis, readiness.ok) == (True, False, False)


async def test_in_process_fsm_storage_needs_no_probe(settings: Config) -> None:
    service = _service(settings)
    service.storage = MemoryStorage()
    assert (await service.check_readiness()).ok


async def test_a_hanging_dependency_fails_within_the_budget(
    settings: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "PROBE_TIMEOUT_SECONDS", 0.05)
    async with asyncio.timeout(1):
        readiness = await _service(
            settings, postgres=_hang, redis=_hang, fsm=_hang, tasks=_hang
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


async def test_report_names_the_commit_backlog_queue_and_heartbeat(
    settings: Config,
) -> None:
    service = _service(settings, pending=(2, timedelta(seconds=30)))
    report = await service.get_report()
    assert report.status == "ok"
    assert report.commit == settings.app.COMMIT_SHA
    assert (report.outbox_pending, report.outbox_oldest_pending_seconds) == (2, 30.0)
    assert report.task_queue_length == 3
    assert report.scheduler_heartbeat_age_seconds == 10.0


async def test_report_degrades_when_the_scheduler_is_silent(settings: Config) -> None:
    """Without the scheduler no cron fires, no retry runs and the outbox is
    never swept, while the bot itself answers every update."""
    stale = timedelta(seconds=HEARTBEAT_TTL_SECONDS + 1)
    report = await _service(settings, beat_age=stale).get_report()
    assert report.status == "degraded"


async def test_report_degrades_when_no_scheduler_ever_beat(settings: Config) -> None:
    report = await _service(settings, beat_age=None).get_report()
    assert (report.status, report.scheduler_heartbeat_age_seconds) == ("degraded", None)


async def test_report_degrades_when_the_outbox_is_stuck(settings: Config) -> None:
    """The sweeper retries every minute; rows pending for minutes mean
    nothing reaches the worker."""
    stuck = timedelta(seconds=services.OUTBOX_STALE_SECONDS + 1)
    report = await _service(settings, pending=(1, stuck)).get_report()
    assert report.status == "degraded"


async def test_an_unreadable_backlog_does_not_break_the_report(
    settings: Config,
) -> None:
    """Monitoring reads /health to learn what is down; a failed count must
    leave the rest of the report standing."""
    service = _service(settings)
    service.outbox.count_pending = AsyncMock(side_effect=OSError("reset"))
    report = await service.get_report()
    assert (report.outbox_pending, report.outbox_oldest_pending_seconds) == (None, None)
    assert report.scheduler_heartbeat_age_seconds == 10.0


async def test_nothing_is_read_from_a_dependency_that_is_down(settings: Config) -> None:
    service = _service(settings, postgres=OSError("down"), tasks=OSError("down"))
    report = await service.get_report()
    service.outbox.count_pending.assert_not_awaited()
    assert (report.outbox_pending, report.task_queue_length) == (None, None)
