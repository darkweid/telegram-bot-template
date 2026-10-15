import asyncio
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.core.cache.interface import CacheKey
from src.core.cache.memory_cache import InMemoryCache
from src.core.cache.serializer import JsonSerializer
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.telegram.roles import Roles
from src.core.utils.datetime_utils import get_utc_now
from src.main.config import Config
from src.user.cache import user_cache_key
from src.user.models import User
from src.user.repositories import UserRepository
from src.user.schemas import TelegramProfile, UserProfile
from src.user.usecases.change_ban import ChangeBanUseCase
from src.user.usecases.touch_user import TouchUserUseCase
from tests.integration.conftest import new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")

ADMIN = 1


class GatedCache(InMemoryCache):
    """Holds the first write until the test opens the gate."""

    def __init__(self) -> None:
        super().__init__(JsonSerializer(), default_ttl=60, version_ttl=600)
        self.writing = asyncio.Event()
        self.gate = asyncio.Event()

    async def set(self, key: CacheKey, value: Any, *, ttl: int | None = None) -> None:
        self.writing.set()
        await self.gate.wait()
        await super().set(key, value, ttl=ttl)


async def test_a_ban_during_a_touch_is_not_undone_by_its_snapshot(
    integration_engine: AsyncEngine, settings: Config
) -> None:
    """The touch read the row before the ban. If its snapshot reached the cache
    after the ban dropped it, the banned user would be served until the
    snapshot expired: the ban's UPDATE must wait for the touch's write."""
    user_id = new_user_id()
    profile = TelegramProfile(
        id=user_id, first_name="Ann", username=None, last_name=None, language_code="en"
    )
    users = UserRepository()
    cache = GatedCache()
    roles = Roles(admins=frozenset({ADMIN}), developers=frozenset())
    try:
        async with AsyncSession(integration_engine) as setup:
            await users.upsert_from_telegram(setup, profile, get_utc_now())
            await setup.commit()
        async with (
            AsyncSession(integration_engine, expire_on_commit=False) as touching,
            AsyncSession(integration_engine, expire_on_commit=False) as banning,
        ):
            touch = TouchUserUseCase(
                SQLAlchemyUnitOfWork(touching), users, cache, settings.bot
            )
            ban = ChangeBanUseCase(SQLAlchemyUnitOfWork(banning), users, cache, roles)
            touch_task = asyncio.create_task(touch.execute(profile))
            ban_task: asyncio.Task[Any] | None = None
            try:
                await asyncio.wait_for(cache.writing.wait(), 10)
                ban_task = asyncio.create_task(ban.execute(ADMIN, user_id, banned=True))
                await asyncio.sleep(0.5)
                assert not ban_task.done()

                cache.gate.set()
                touched = await asyncio.wait_for(touch_task, 10)
                change = await asyncio.wait_for(ban_task, 10)
            finally:
                cache.gate.set()
                for task in (touch_task, ban_task):
                    if task is not None and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

        assert touched.is_banned is False
        assert change.changed is True
        assert await cache.get(user_cache_key(user_id), model=UserProfile) is None
        async with AsyncSession(
            integration_engine, expire_on_commit=False
        ) as next_update:
            again = TouchUserUseCase(
                SQLAlchemyUnitOfWork(next_update), users, cache, settings.bot
            )
            assert (await again.execute(profile)).is_banned is True
    finally:
        async with AsyncSession(integration_engine) as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
