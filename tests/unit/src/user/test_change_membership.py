from unittest.mock import AsyncMock

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.user.repositories import UserRepository
from src.user.schemas import TelegramProfile
from src.user.usecases.change_membership import ChangeMembershipUseCase
from tests.fakes.db import FakeAsyncSession

PROFILE = TelegramProfile(
    id=1, first_name="Ann", username=None, last_name=None, language_code=None
)


def _use_case(session: FakeAsyncSession, users: AsyncMock) -> ChangeMembershipUseCase:
    return ChangeMembershipUseCase(SQLAlchemyUnitOfWork(session), users)


async def test_kicking_the_bot_marks_the_user_unreachable() -> None:
    session, users = FakeAsyncSession(), AsyncMock(spec=UserRepository)
    await _use_case(session, users).execute(PROFILE, is_member=False)
    assert users.set_reachable.await_args.args[1:3] == (1, False)
    users.upsert_from_telegram.assert_not_awaited()
    session.commit.assert_awaited_once()


async def test_restarting_the_bot_stores_and_marks_the_user_reachable() -> None:
    session, users = FakeAsyncSession(), AsyncMock(spec=UserRepository)
    await _use_case(session, users).execute(PROFILE, is_member=True)
    assert users.upsert_from_telegram.await_args.args[1] == PROFILE
    users.set_reachable.assert_not_awaited()
    session.commit.assert_awaited_once()
