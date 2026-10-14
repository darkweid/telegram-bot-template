from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage

from src.core.telegram.fsm import create_event_isolation, create_fsm_storage
from src.main.config import Config


async def test_fsm_storage_uses_its_own_database_and_bot_scoped_keys(
    settings: Config,
) -> None:
    storage = create_fsm_storage(settings.redis.fsm_dsn)
    try:
        assert isinstance(storage, RedisStorage)
        kwargs = storage.redis.connection_pool.connection_kwargs
        assert str(kwargs["db"]) == settings.redis.REDIS_FSM_DATABASE
        assert isinstance(storage.key_builder, DefaultKeyBuilder)
        assert storage.key_builder.with_bot_id is True
    finally:
        await storage.close()


async def test_isolation_follows_the_storage(settings: Config) -> None:
    redis_storage = create_fsm_storage(settings.redis.fsm_dsn)
    try:
        assert isinstance(create_event_isolation(redis_storage), RedisEventIsolation)
    finally:
        await redis_storage.close()
    assert isinstance(create_event_isolation(MemoryStorage()), SimpleEventIsolation)
