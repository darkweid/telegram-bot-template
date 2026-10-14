from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage

from src.core.telegram.fsm import (
    EVENT_ISOLATION_LOCK_SECONDS,
    create_event_isolation,
    create_fsm_storage,
)
from src.core.telegram.session import MAX_RETRY_SLEEP_SECONDS
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


async def test_the_isolation_lock_outlives_a_full_flood_wait(settings: Config) -> None:
    # An update sleeping out flood control still holds the user's lock; a lock
    # that expired under it would let the next update run concurrently. All
    # sleeps of one update share MAX_RETRY_SLEEP_SECONDS, and half the lock
    # is left for the handler's own work.
    storage = create_fsm_storage(settings.redis.fsm_dsn)
    try:
        isolation = create_event_isolation(storage)
        assert isinstance(isolation, RedisEventIsolation)
        assert isolation.lock_kwargs["timeout"] == EVENT_ISOLATION_LOCK_SECONDS
        assert isolation.lock_kwargs["raise_on_release_error"] is False
    finally:
        await storage.close()
    assert MAX_RETRY_SLEEP_SECONDS * 2 <= EVENT_ISOLATION_LOCK_SECONDS
