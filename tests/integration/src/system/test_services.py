import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.redis.core import create_redis_client
from src.core.tasks.broker import TasksRedis
from src.core.telegram.fsm import create_fsm_storage
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
        SystemRepository(), session, redis, storage, tasks, config.app, config.bot
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
