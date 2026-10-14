from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.outbox.repositories import OutboxRepository
from src.core.redis.core import create_redis_client
from src.core.tasks.broker import STREAM_CONSUMER_GROUP, STREAM_QUEUE_NAME, TasksRedis
from src.core.tasks.heartbeat import LAST_BEAT_KEY
from src.core.telegram.fsm import create_fsm_storage
from src.core.utils.datetime_utils import get_utc_now
from src.main.config import Config
from src.system.repositories import SystemRepository
from src.system.services import HealthService

# The integration marker comes from tests/integration/conftest.py; the loop
# scope must match db_session's.
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _readiness(
    config: Config,
    session: AsyncSession,
    fsm_database: int,
    tasks_database: int | None = None,
) -> bool:
    redis_config = config.redis.model_copy(
        update={
            "REDIS_FSM_DATABASE": fsm_database,
            "REDIS_TASKS_DATABASE": (
                config.redis.REDIS_TASKS_DATABASE
                if tasks_database is None
                else tasks_database
            ),
        }
    )
    redis = create_redis_client(config.redis.dsn)
    storage = create_fsm_storage(redis_config.fsm_dsn)
    tasks = TasksRedis(create_redis_client(redis_config.tasks_dsn))
    service = HealthService(
        SystemRepository(),
        OutboxRepository(),
        session,
        redis,
        storage,
        tasks,
        config.app,
        config.bot,
    )
    try:
        return (await service.check_readiness()).redis
    finally:
        await tasks.aclose()
        await storage.close()
        await redis.aclose()


async def test_an_fsm_database_the_server_refuses_fails_readiness(
    settings: Config, db_session: AsyncSession
) -> None:
    """Redis connects lazily and selects the database on connect: only a
    command on the FSM client itself finds out the index is out of range."""
    assert await _readiness(settings, db_session, settings.redis.REDIS_FSM_DATABASE)
    assert not await _readiness(settings, db_session, 100_000)


async def test_a_task_database_the_server_refuses_fails_readiness(
    settings: Config, db_session: AsyncSession
) -> None:
    assert not await _readiness(
        settings, db_session, settings.redis.REDIS_FSM_DATABASE, tasks_database=100_000
    )


@asynccontextmanager
async def _health(
    settings: Config, session: AsyncSession
) -> AsyncIterator[tuple[HealthService, TasksRedis]]:
    """The service on real clients, with the task stream emptied before and
    after, so no other test's tasks count."""
    redis = create_redis_client(settings.redis.dsn)
    storage = create_fsm_storage(settings.redis.fsm_dsn)
    tasks = TasksRedis(create_redis_client(settings.redis.tasks_dsn))
    await tasks.delete(STREAM_QUEUE_NAME)
    try:
        yield (
            HealthService(
                SystemRepository(),
                OutboxRepository(),
                session,
                redis,
                storage,
                tasks,
                settings.app,
                settings.bot,
            ),
            tasks,
        )
    finally:
        await tasks.delete(STREAM_QUEUE_NAME, LAST_BEAT_KEY)
        await tasks.aclose()
        await storage.close()
        await redis.aclose()


async def test_the_report_reads_the_queue_and_the_heartbeat(
    settings: Config, db_session: AsyncSession
) -> None:
    """A stream nobody created yet has nothing waiting, and the heartbeat
    comes back as a string; both must reach the report as numbers."""
    async with _health(settings, db_session) as (service, tasks):
        await tasks.set(LAST_BEAT_KEY, get_utc_now().isoformat())
        report = await service.get_report()
    assert report.task_queue_length == 0
    assert report.scheduler_heartbeat_age_seconds is not None
    assert 0 <= report.scheduler_heartbeat_age_seconds < 60
    assert report.outbox_pending is not None
    # /status reads the report inside an update; an open transaction would sit
    # idle on the update's connection while the reply is sent.
    assert not db_session.in_transaction()


async def test_an_acked_task_leaves_the_queue(
    settings: Config, db_session: AsyncSession
) -> None:
    """Acked entries stay in the stream until MAXLEN trims them: the queue is
    what the workers' group has not delivered or not acked, never XLEN."""
    async with _health(settings, db_session) as (service, tasks):
        await tasks.xgroup_create(
            STREAM_QUEUE_NAME, STREAM_CONSUMER_GROUP, id="$", mkstream=True
        )
        await tasks.xadd(STREAM_QUEUE_NAME, {"data": "first"})
        await tasks.xadd(STREAM_QUEUE_NAME, {"data": "second"})
        undelivered = (await service.get_report()).task_queue_length
        [[_, [(delivered_id, _)]]] = await tasks.xreadgroup(
            STREAM_CONSUMER_GROUP, "worker", {STREAM_QUEUE_NAME: ">"}, count=1
        )
        delivered = (await service.get_report()).task_queue_length
        await tasks.xack(STREAM_QUEUE_NAME, STREAM_CONSUMER_GROUP, delivered_id)
        acked = (await service.get_report()).task_queue_length
        length = await tasks.xlen(STREAM_QUEUE_NAME)
    assert (undelivered, delivered, acked) == (2, 2, 1)
    assert length == 2
