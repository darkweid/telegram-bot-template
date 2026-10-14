from unittest.mock import AsyncMock

import pytest

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.user.exceptions import UnsupportedLocaleError
from src.user.repositories import UserRepository
from src.user.usecases.set_locale import SetLocaleUseCase
from tests.fakes.db import FakeAsyncSession


def _use_case(session: FakeAsyncSession, users: AsyncMock) -> SetLocaleUseCase:
    return SetLocaleUseCase(SQLAlchemyUnitOfWork(session), users)  # type: ignore[arg-type]


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
