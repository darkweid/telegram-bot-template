from aiogram.fsm.storage.base import StorageKey

from src.core.telegram.fsm import create_fsm_storage
from src.main.config import Config


async def test_closing_a_used_fsm_storage_twice_is_harmless(settings: Config) -> None:
    """The container closes the storage after the dispatcher already has."""
    storage = create_fsm_storage(settings.redis.fsm_dsn)
    key = StorageKey(bot_id=1, chat_id=2, user_id=4)
    try:
        await storage.set_state(key, "Form:name")
        assert await storage.get_state(key) == "Form:name"
    finally:
        await storage.set_state(key, None)
        await storage.close()
    await storage.close()
