import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.outbox.repositories import OutboxRepository
from src.core.redis.core import create_redis_client
from src.core.tasks.broker import TasksRedis
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
    fsm_database: str,
    tasks_database: str | None = None,
) -> bool:
    redis_config = config.redis.model_copy(
        update={
            "REDIS_FSM_DATABASE": fsm_database,
            "REDIS_TASKS_DATABASE": tasks_database or config.redis.REDIS_TASKS_DATABASE,
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
    assert not await _readiness(settings, db_session, "100000")


async def test_a_task_database_the_server_refuses_fails_readiness(
    settings: Config, db_session: AsyncSession
) -> None:
    assert not await _readiness(
        settings, db_session, settings.redis.REDIS_FSM_DATABASE, tasks_database="100000"
    )


async def test_the_report_reads_the_queue_and_the_heartbeat(
    settings: Config, db_session: AsyncSession
) -> None:
    """XLEN of a stream nobody created yet is 0, and the heartbeat comes back
    as bytes; both must reach the report as numbers."""
    redis = create_redis_client(settings.redis.dsn)
    storage = create_fsm_storage(settings.redis.fsm_dsn)
    tasks = TasksRedis(create_redis_client(settings.redis.tasks_dsn))
    await tasks.set(LAST_BEAT_KEY, get_utc_now().isoformat())
    service = HealthService(
        SystemRepository(),
        OutboxRepository(),
        db_session,
        redis,
        storage,
        tasks,
        settings.app,
        settings.bot,
    )
    try:
        report = await service.get_report()
    finally:
        await tasks.delete(LAST_BEAT_KEY)
        await tasks.aclose()
        await storage.close()
        await redis.aclose()
    assert report.task_queue_length is not None and report.task_queue_length >= 0
    assert report.scheduler_heartbeat_age_seconds is not None
    assert 0 <= report.scheduler_heartbeat_age_seconds < 60
    assert report.outbox_pending is not None
    # /status reads the report inside an update; an open transaction would sit
    # idle on the update's connection while the reply is sent.
    assert not db_session.in_transaction()
