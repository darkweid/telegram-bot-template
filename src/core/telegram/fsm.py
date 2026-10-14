from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage, DefaultKeyBuilder
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage

from src.core.redis.core import create_redis_client

# Twice the flood-wait sleep one update may take (MAX_RETRY_SLEEP_SECONDS in
# session.py): the other half is the handler's own work.
EVENT_ISOLATION_LOCK_SECONDS = 60


def create_fsm_storage(dsn: str) -> RedisStorage:
    """Dialog state in Redis, so it survives a restart and is shared by replicas.

    The DSN points at its own database (REDIS_FSM_DATABASE): flushing a cache
    must never drop a user in the middle of a dialog. Keys carry the bot id,
    so two bots on one Redis do not read each other's state.
    """
    return RedisStorage(
        redis=create_redis_client(dsn),
        key_builder=DefaultKeyBuilder(with_bot_id=True),
    )


def create_event_isolation(storage: BaseStorage) -> BaseEventIsolation:
    """Serialize updates of one user and chat.

    Two quick taps on an inline button otherwise run concurrently and race on
    the same FSM state. With Redis storage the lock lives in Redis, so it also
    holds across replicas; any other storage gets an in-process lock.

    A handler that outlives the Redis lock regardless (slow work of its own)
    has already finished, so the lock it no longer owns is let go with
    redis-py's warning. Raising there would tell the user a finished action
    failed, or replace the exception the handler itself raised.
    """
    if isinstance(storage, RedisStorage):
        return RedisEventIsolation(
            redis=storage.redis,
            key_builder=storage.key_builder,
            lock_kwargs={
                "timeout": EVENT_ISOLATION_LOCK_SECONDS,
                "raise_on_release_error": False,
            },
        )
    return SimpleEventIsolation()
