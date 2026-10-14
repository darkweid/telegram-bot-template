from unittest.mock import AsyncMock

import pytest

from src.core.cache.interface import Cache
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.telegram.roles import Roles
from src.user.cache import user_cache_key
from src.user.exceptions import (
    AmbiguousUsernameError,
    ProtectedUserError,
    SelfBanError,
    UserNotFoundError,
)
from src.user.models import User
from src.user.repositories import UserRepository
from src.user.schemas import UserProfile
from src.user.usecases.change_ban import ChangeBanUseCase
from tests.factories.users import build_cache, build_user_model, build_user_snapshot
from tests.fakes.db import FakeAsyncSession

ADMIN = 1
DEVELOPER = 2
ROLES = Roles(admins=frozenset({ADMIN}), developers=frozenset({DEVELOPER}))


def _users(*stored: User) -> AsyncMock:
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = stored[0] if stored else None
    users.find_by_username.return_value = list(stored)
    users.set_banned.return_value = True
    return users


def _use_case(
    users: AsyncMock,
    cache: Cache | None = None,
    session: FakeAsyncSession | None = None,
) -> ChangeBanUseCase:
    return ChangeBanUseCase(
        SQLAlchemyUnitOfWork(session or FakeAsyncSession()),
        users,
        cache or build_cache(),
        ROLES,
    )


async def test_a_ban_is_stored_and_the_snapshot_dropped() -> None:
    """The next update of the banned user must already be dropped."""
    cache = build_cache()
    await cache.set(user_cache_key(42), build_user_snapshot(id=42))
    users = _users(build_user_model(id=42))
    change = await _use_case(users, cache).execute(ADMIN, 42, banned=True)
    assert change.changed and change.user.is_banned
    assert users.set_banned.await_args is not None
    assert users.set_banned.await_args.kwargs["by"] == ADMIN
    assert await cache.get(user_cache_key(42), model=UserProfile) is None


async def test_a_failed_commit_keeps_the_snapshot() -> None:
    """An eager drop would let a concurrent update cache the unbanned row again
    before the ban is visible; the drop waits for a successful commit."""
    cache = build_cache()
    await cache.set(user_cache_key(42), build_user_snapshot(id=42))
    session = FakeAsyncSession()
    session.commit.side_effect = ConnectionError("database down")
    with pytest.raises(ConnectionError):
        await _use_case(_users(build_user_model(id=42)), cache, session).execute(
            ADMIN, 42, banned=True
        )
    assert await cache.get(user_cache_key(42), model=UserProfile) is not None


async def test_banning_a_banned_user_changes_nothing() -> None:
    users = _users(build_user_model(id=42, is_banned=True))
    change = await _use_case(users).execute(ADMIN, 42, banned=True)
    assert not change.changed
    users.set_banned.assert_not_awaited()


async def test_ban_refuses_a_privileged_target() -> None:
    """An admin who could ban another admin or a developer would lock them
    out of the commands they need to undo it."""
    users = _users(build_user_model(id=DEVELOPER))
    with pytest.raises(ProtectedUserError):
        await _use_case(users).execute(ADMIN, DEVELOPER, banned=True)
    users.set_banned.assert_not_awaited()


async def test_an_admin_cannot_ban_themselves() -> None:
    with pytest.raises(SelfBanError):
        await _use_case(_users(build_user_model(id=ADMIN))).execute(
            ADMIN, ADMIN, banned=True
        )


async def test_a_user_who_never_started_the_bot_is_not_found() -> None:
    with pytest.raises(UserNotFoundError):
        await _use_case(_users()).execute(ADMIN, 42, banned=True)


async def test_a_username_held_by_two_rows_is_refused() -> None:
    """Banning the wrong person is worse than asking for the id."""
    users = _users(
        build_user_model(id=42, username="ann_smith"),
        build_user_model(id=43, username="ann_smith"),
    )
    with pytest.raises(AmbiguousUsernameError):
        await _use_case(users).execute(ADMIN, "ann_smith", banned=True)
    users.set_banned.assert_not_awaited()


async def test_a_username_held_by_one_row_is_banned() -> None:
    users = _users(build_user_model(id=42, username="ann_smith"))
    change = await _use_case(users).execute(ADMIN, "ann_smith", banned=True)
    assert (change.user.id, change.changed) == (42, True)


async def test_an_unban_of_a_privileged_user_is_allowed() -> None:
    """A stale ban on someone who later got a role may be cleaned up."""
    users = _users(build_user_model(id=DEVELOPER, is_banned=True))
    change = await _use_case(users).execute(ADMIN, DEVELOPER, banned=False)
    assert change.changed and not change.user.is_banned
