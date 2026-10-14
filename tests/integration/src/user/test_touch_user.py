from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from src.core.cache.memory_cache import InMemoryCache
from src.core.cache.serializer import JsonSerializer
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.main.config import Config
from src.user.models import User
from src.user.repositories import UserRepository
from src.user.schemas import TelegramProfile
from src.user.usecases import touch_user as module
from src.user.usecases.touch_user import TouchUserUseCase
from tests.integration.conftest import new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_touch_writes_only_when_needed(
    integration_engine: AsyncEngine, settings: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    user_id = new_user_id()
    profile = TelegramProfile(
        id=user_id, first_name="Ann", username=None, last_name=None, language_code="en"
    )
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    # Disabled: this test is about which rows the database writes, so every
    # touch must reach it.
    cache = InMemoryCache(
        JsonSerializer(), default_ttl=60, version_ttl=600, enabled=False
    )

    async def touch(at: datetime) -> datetime:
        monkeypatch.setattr(module, "get_utc_now", lambda: at)
        async with sessions() as session:
            use_case = TouchUserUseCase(
                SQLAlchemyUnitOfWork(session), UserRepository(), cache, settings.bot
            )
            result = await use_case.execute(profile)
        return result.last_seen_at

    try:
        assert await touch(t0) == t0
        assert await touch(t0 + timedelta(seconds=10)) == t0  # within the interval
        later = t0 + timedelta(seconds=settings.bot.USER_TOUCH_INTERVAL_SECONDS)
        assert await touch(later) == later
    finally:
        async with sessions() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
