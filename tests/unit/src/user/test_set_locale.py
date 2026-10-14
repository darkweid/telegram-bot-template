from unittest.mock import AsyncMock

import pytest

from src.core.cache.interface import Cache
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.user.cache import user_cache_key
from src.user.exceptions import UnsupportedLocaleError
from src.user.repositories import UserRepository
from src.user.schemas import UserProfile
from src.user.usecases.set_locale import SetLocaleUseCase
from tests.factories.users import build_cache, build_user_snapshot
from tests.fakes.db import FakeAsyncSession


def _use_case(
    session: FakeAsyncSession, users: AsyncMock, cache: Cache | None = None
) -> SetLocaleUseCase:
    return SetLocaleUseCase(
        SQLAlchemyUnitOfWork(session), users, cache or build_cache()
    )


async def test_saves_a_supported_locale() -> None:
    session, users = FakeAsyncSession(), AsyncMock(spec=UserRepository)
    users.set_locale.return_value = True
    await _use_case(session, users).execute(1, "ru")
    users.set_locale.assert_awaited_once_with(session, 1, "ru")
    session.commit.assert_awaited_once()


async def test_refuses_an_unsupported_locale_without_touching_the_database() -> None:
    session, users = FakeAsyncSession(), AsyncMock(spec=UserRepository)
    with pytest.raises(UnsupportedLocaleError) as raised:
        await _use_case(session, users).execute(1, "xx")
    assert raised.value.detail is None  # the callback value is never logged
    users.set_locale.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_a_saved_locale_drops_the_cached_snapshot() -> None:
    """The next update would otherwise answer in the old language for up to
    USER_CACHE_TTL_SECONDS."""
    cache = build_cache()
    await cache.set(user_cache_key(1), build_user_snapshot())
    session, users = FakeAsyncSession(), AsyncMock(spec=UserRepository)
    users.set_locale.return_value = True
    await _use_case(session, users, cache).execute(1, "ru")
    assert await cache.get(user_cache_key(1), model=UserProfile) is None


async def test_a_failed_locale_change_keeps_the_snapshot() -> None:
    """Dropped before the commit, the snapshot could be cached again from the
    old row by a concurrent update; nothing changed, so nothing is dropped."""
    cache = build_cache()
    await cache.set(user_cache_key(1), build_user_snapshot())
    session, users = FakeAsyncSession(), AsyncMock(spec=UserRepository)
    users.set_locale.side_effect = ConnectionError("database down")
    with pytest.raises(ConnectionError):
        await _use_case(session, users, cache).execute(1, "ru")
    assert await cache.get(user_cache_key(1), model=UserProfile) is not None
