from aiogram.fsm.storage.base import StorageKey

from src.core.telegram.fsm import create_event_isolation, create_fsm_storage
from src.main.config import Config


async def test_state_and_data_survive_a_round_trip(settings: Config) -> None:
    storage = create_fsm_storage(settings.redis.fsm_dsn)
    key = StorageKey(bot_id=1, chat_id=2, user_id=3)
    try:
        await storage.set_state(key, "Form:name")
        await storage.set_data(key, {"step": 1, "name": "Ann"})
        assert await storage.get_state(key) == "Form:name"
        assert await storage.get_data(key) == {"step": 1, "name": "Ann"}
    finally:
        await storage.set_state(key, None)
        await storage.set_data(key, {})
        await storage.close()


async def test_a_lock_lost_under_a_slow_update_does_not_fail_it(
    settings: Config,
) -> None:
    storage = create_fsm_storage(settings.redis.fsm_dsn)
    isolation = create_event_isolation(storage)
    key = StorageKey(bot_id=1, chat_id=2, user_id=3)
    try:
        async with isolation.lock(key):
            # What the lock's expiry does to an update still running.
            await storage.redis.delete(storage.key_builder.build(key, "lock"))
    finally:
        await storage.close()
