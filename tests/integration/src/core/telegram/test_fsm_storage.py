from aiogram.fsm.storage.base import StorageKey

from src.core.telegram.fsm import create_fsm_storage
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
