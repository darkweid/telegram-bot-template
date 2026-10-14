from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage, DefaultKeyBuilder
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.fsm.storage.redis import RedisStorage

from src.core.redis.core import create_redis_client


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
    """
    if isinstance(storage, RedisStorage):
        return storage.create_isolation()
    return SimpleEventIsolation()
