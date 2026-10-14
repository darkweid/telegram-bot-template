from unittest.mock import AsyncMock

import pytest

from src.core.cache.interface import Cache
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.registry import set_event_subscribers
from src.core.events.schemas import Actor, PublishedEvent
from src.user.cache import user_cache_key
from src.user.events import UserRegistered
from src.user.repositories import UpsertedUser, UserRepository
from src.user.schemas import TelegramProfile, UserProfile
from src.user.usecases.change_membership import ChangeMembershipUseCase
from tests.factories.users import build_cache, build_user_model, build_user_snapshot
from tests.fakes.db import FakeAsyncSession

PROFILE = TelegramProfile(
    id=1, first_name="Ann", username=None, last_name=None, language_code=None
)


def _use_case(
    session: FakeAsyncSession, users: AsyncMock, cache: Cache | None = None
) -> ChangeMembershipUseCase:
    return ChangeMembershipUseCase(
        SQLAlchemyUnitOfWork(session), users, cache or build_cache()
    )


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


@pytest.mark.parametrize("is_member", [True, False])
async def test_a_membership_change_drops_the_cached_snapshot(is_member: bool) -> None:
    """The snapshot carries is_reachable; a broadcast or a ban check must see
    the change at once."""
    cache = build_cache()
    await cache.set(user_cache_key(1), build_user_snapshot())
    session, users = FakeAsyncSession(), AsyncMock(spec=UserRepository)
    await _use_case(session, users, cache).execute(PROFILE, is_member=is_member)
    assert await cache.get(user_cache_key(1), model=UserProfile) is None


@pytest.mark.parametrize(
    ("is_member", "failing"),
    [(True, "upsert_from_telegram"), (False, "set_reachable")],
)
async def test_a_failed_membership_change_keeps_the_snapshot(
    is_member: bool, failing: str
) -> None:
    """Dropped before the commit, the snapshot could be cached again from the
    old row by a concurrent update; nothing changed, so nothing is dropped."""
    cache = build_cache()
    await cache.set(user_cache_key(1), build_user_snapshot())
    session, users = FakeAsyncSession(), AsyncMock(spec=UserRepository)
    getattr(users, failing).side_effect = ConnectionError("database down")
    with pytest.raises(ConnectionError):
        await _use_case(session, users, cache).execute(PROFILE, is_member=is_member)
    assert await cache.get(user_cache_key(1), model=UserProfile) is not None


@pytest.mark.parametrize("is_member", [True, False])
async def test_a_failed_commit_keeps_the_snapshot(is_member: bool) -> None:
    """An eager drop would let a concurrent update cache the old row again
    before the change is visible; the drop waits for a successful commit."""
    cache = build_cache()
    await cache.set(user_cache_key(1), build_user_snapshot())
    session, users = FakeAsyncSession(), AsyncMock(spec=UserRepository)
    session.commit.side_effect = ConnectionError("database down")
    with pytest.raises(ConnectionError):
        await _use_case(session, users, cache).execute(PROFILE, is_member=is_member)
    assert await cache.get(user_cache_key(1), model=UserProfile) is not None


@pytest.fixture
def published() -> list[PublishedEvent]:
    """Record what the use case publishes; the root conftest restores the
    registry."""
    seen: list[PublishedEvent] = []

    async def record(_uow: SQLAlchemyUnitOfWork, event: PublishedEvent) -> None:
        seen.append(event)

    set_event_subscribers({UserRegistered: [record]})
    return seen


async def test_a_user_first_stored_by_a_membership_change_is_registered(
    published: list[PublishedEvent],
) -> None:
    """Telegram can report "member" before the user's first message; without
    the event the admins would never hear of that user."""
    session, users = FakeAsyncSession(), AsyncMock(spec=UserRepository)
    users.upsert_from_telegram.return_value = UpsertedUser(
        user=build_user_model(id=PROFILE.id), created=True
    )
    await _use_case(session, users).execute(PROFILE, is_member=True)
    [event] = published
    assert event.event == UserRegistered(user_id=PROFILE.id)
    assert event.actor == Actor.telegram_user(PROFILE.id)
    session.commit.assert_awaited_once()


async def test_a_known_user_rejoining_is_not_registered_again(
    published: list[PublishedEvent],
) -> None:
    """An unblock is no new user; admins would get a notice for every one."""
    session, users = FakeAsyncSession(), AsyncMock(spec=UserRepository)
    users.upsert_from_telegram.return_value = UpsertedUser(
        user=build_user_model(id=PROFILE.id), created=False
    )
    await _use_case(session, users).execute(PROFILE, is_member=True)
    assert published == []
