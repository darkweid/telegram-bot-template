import asyncio
from collections.abc import AsyncIterator
import contextlib
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.core.utils.datetime_utils import get_utc_now
from src.user.models import User
from src.user.repositories import UserRepository
from src.user.schemas import TelegramProfile
from tests.integration.conftest import build_user_data, new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def user_id() -> int:
    return new_user_id()


# Every async fixture here runs on the session loop, like db_session: an asyncpg
# connection cannot cross event loops.
@pytest_asyncio.fixture(loop_scope="session")
async def cleanup(db_session: AsyncSession, user_id: int) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(delete(User).where(User.id == user_id))
    await db_session.commit()


def _profile(user_id: int, **overrides: object) -> TelegramProfile:
    fields: dict[str, object] = {
        "id": user_id,
        "first_name": "Ann",
        "username": "ann",
        "last_name": None,
        "language_code": "ru",
    }
    return TelegramProfile(**(fields | overrides))


async def test_first_upsert_creates_the_user(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    upserted = await UserRepository().upsert_from_telegram(
        db_session, _profile(user_id), T0
    )
    await db_session.commit()
    assert upserted.created is True
    assert (upserted.user.id, upserted.user.first_name, upserted.user.last_seen_at) == (
        user_id,
        "Ann",
        T0,
    )
    assert upserted.user.is_reachable is True


async def test_second_upsert_updates_profile_and_last_seen(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    repository = UserRepository()
    await repository.upsert_from_telegram(db_session, _profile(user_id), T0)
    await db_session.commit()
    later = T0 + timedelta(hours=1)
    changed = _profile(
        user_id,
        first_name="Anna",
        last_name="Lee",
        username="ann2",
        language_code="en",
    )
    upserted = await repository.upsert_from_telegram(db_session, changed, later)
    await db_session.commit()
    assert upserted.created is False
    user = upserted.user
    assert (
        user.first_name,
        user.last_name,
        user.username,
        user.language_code,
        user.last_seen_at,
    ) == ("Anna", "Lee", "ann2", "en", later)
    # Both stamps are PostgreSQL's now() (TimestampMixin's default and the
    # upsert's SET both use func.now()), taken in two transactions: one clock,
    # and the second transaction starts after the first committed.
    assert upserted.user.updated_at > upserted.user.created_at


async def test_upsert_keeps_the_chosen_locale(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    repository = UserRepository()
    await repository.upsert_from_telegram(db_session, _profile(user_id), T0)
    assert await repository.set_locale(db_session, user_id, "en") is True
    await db_session.commit()
    upserted = await repository.upsert_from_telegram(
        db_session, _profile(user_id, language_code="de"), T0
    )
    await db_session.commit()
    assert (upserted.user.locale, upserted.user.language_code) == ("en", "de")


async def test_unreachable_keeps_the_first_time_and_upsert_makes_reachable(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    repository = UserRepository()
    await repository.upsert_from_telegram(db_session, _profile(user_id), T0)
    assert (
        await repository.set_reachable(
            db_session, user_id, False, T0 + timedelta(minutes=1)
        )
        is True
    )
    assert (
        await repository.set_reachable(
            db_session, user_id, False, T0 + timedelta(minutes=2)
        )
        is True
    )
    await db_session.commit()
    unreachable = await repository.get_single(
        db_session, id=user_id, populate_existing=True
    )
    assert unreachable is not None
    assert (unreachable.is_reachable, unreachable.unreachable_since) == (
        False,
        T0 + timedelta(minutes=1),
    )

    upserted = await repository.upsert_from_telegram(
        db_session, _profile(user_id), T0 + timedelta(minutes=3)
    )
    await db_session.commit()
    assert (upserted.user.is_reachable, upserted.user.unreachable_since) == (True, None)


async def test_reachable_again_clears_the_unreachable_time(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    repository = UserRepository()
    await repository.upsert_from_telegram(db_session, _profile(user_id), T0)
    await repository.set_reachable(db_session, user_id, False, T0)
    await repository.set_reachable(db_session, user_id, True, T0)
    await db_session.commit()
    user = await repository.get_single(db_session, id=user_id, populate_existing=True)
    assert user is not None
    assert (user.is_reachable, user.unreachable_since) == (True, None)


async def test_writes_to_an_unknown_user_report_false(
    db_session: AsyncSession, user_id: int
) -> None:
    repository = UserRepository()
    assert await repository.set_reachable(db_session, user_id, False, T0) is False
    assert await repository.set_locale(db_session, user_id, "en") is False
    assert not await repository.set_banned(
        db_session, user_id, banned=True, by=7, now=T0
    )
    await db_session.rollback()


async def test_a_concurrent_first_upsert_waits_and_then_updates(
    integration_engine: AsyncEngine, user_id: int
) -> None:
    """Two updates of a new user arriving together: the second must not fail with
    IntegrityError, it must wait for the first and take the update branch."""
    repository = UserRepository()
    try:
        async with (
            AsyncSession(integration_engine, expire_on_commit=False) as first,
            AsyncSession(integration_engine, expire_on_commit=False) as second,
        ):
            inserted = await repository.upsert_from_telegram(
                first, _profile(user_id), T0
            )
            assert inserted.created is True

            task = asyncio.create_task(
                repository.upsert_from_telegram(
                    second, _profile(user_id, username="ann2"), T0
                )
            )
            try:
                await asyncio.sleep(0.5)
                assert not task.done()

                await first.commit()
                upserted = await asyncio.wait_for(task, 10)
                await second.commit()
            finally:
                # On any failure above the second upsert is still pending: end it
                # before its session closes, or its error is reported as noise.
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        assert upserted.created is False
        assert upserted.user.username == "ann2"
    finally:
        async with AsyncSession(integration_engine) as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()


async def test_set_reachable_and_set_locale_move_updated_at(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    repository = UserRepository()
    created = (
        await repository.upsert_from_telegram(db_session, _profile(user_id), T0)
    ).user
    await db_session.commit()
    stamps = [created.updated_at]

    for write in (
        lambda: repository.set_reachable(db_session, user_id, False, T0),
        lambda: repository.set_locale(db_session, user_id, "en"),
    ):
        assert await write() is True
        await db_session.commit()
        user = await repository.get_single(
            db_session, id=user_id, populate_existing=True
        )
        assert user is not None
        stamps.append(user.updated_at)
        await db_session.commit()

    assert stamps == sorted(set(stamps))


async def test_a_ban_records_who_and_when(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    users = UserRepository()
    await users.upsert_from_telegram(db_session, _profile(user_id), T0)
    assert await users.set_banned(db_session, user_id, banned=True, by=7, now=T0)
    await db_session.commit()
    user = await users.get_single(db_session, id=user_id, populate_existing=True)
    assert user is not None
    assert (user.is_banned, user.banned_at, user.banned_by) == (True, T0, 7)


async def test_an_unban_keeps_who_touched_it_last(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    users = UserRepository()
    await users.upsert_from_telegram(db_session, _profile(user_id), T0)
    await users.set_banned(db_session, user_id, banned=True, by=7, now=T0)
    await users.set_banned(db_session, user_id, banned=False, by=8, now=T0)
    await db_session.commit()
    user = await users.get_single(db_session, id=user_id, populate_existing=True)
    assert user is not None
    assert (user.is_banned, user.banned_at, user.banned_by) == (False, None, 8)


async def test_a_username_is_found_whatever_its_case(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    """Telegram treats usernames case-insensitively; an admin types them as
    they remember them."""
    users = UserRepository()
    username = f"Ann_{user_id}"
    await users.upsert_from_telegram(
        db_session, _profile(user_id, username=username), T0
    )
    await db_session.commit()
    [found] = await users.find_by_username(db_session, username.upper())
    assert found.id == user_id


async def test_a_refresh_keeps_the_ban(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    """Every update upserts the profile; a ban must survive it."""
    users = UserRepository()
    await users.upsert_from_telegram(db_session, _profile(user_id), T0)
    await users.set_banned(db_session, user_id, banned=True, by=7, now=T0)
    upserted = await users.upsert_from_telegram(
        db_session, _profile(user_id, first_name="New"), T0 + timedelta(minutes=1)
    )
    await db_session.commit()
    assert upserted.user.is_banned is True


async def test_a_repeated_ban_reports_no_change(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    """The admin is told the user already was banned, and `banned_at` keeps the
    first ban's time."""
    users = UserRepository()
    await users.upsert_from_telegram(db_session, _profile(user_id), T0)
    assert await users.set_banned(db_session, user_id, banned=True, by=7, now=T0)
    later = T0 + timedelta(minutes=1)
    assert not await users.set_banned(db_session, user_id, banned=True, by=8, now=later)
    await db_session.commit()
    user = await users.get_single(db_session, id=user_id, populate_existing=True)
    assert user is not None
    assert (user.banned_at, user.banned_by) == (T0, 7)


async def test_of_two_concurrent_bans_only_one_changes_the_user(
    integration_engine: AsyncEngine, user_id: int
) -> None:
    """Two admins banning at once must not both be told they banned the user:
    the second waits for the first and finds nothing left to change."""
    repository = UserRepository()
    try:
        async with AsyncSession(integration_engine) as setup:
            await repository.upsert_from_telegram(setup, _profile(user_id), T0)
            await setup.commit()
        async with (
            AsyncSession(integration_engine, expire_on_commit=False) as first,
            AsyncSession(integration_engine, expire_on_commit=False) as second,
        ):
            assert await repository.set_banned(
                first, user_id, banned=True, by=7, now=T0
            )
            task = asyncio.create_task(
                repository.set_banned(second, user_id, banned=True, by=8, now=T0)
            )
            try:
                await asyncio.sleep(0.5)
                assert not task.done()

                await first.commit()
                changed = await asyncio.wait_for(task, 10)
                await second.commit()
            finally:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        assert changed is False
    finally:
        async with AsyncSession(integration_engine) as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()


async def test_recipients_are_reachable_unbanned_users_in_id_order(
    db_session: AsyncSession,
) -> None:
    """The broadcast walks this keyset; a banned or unreachable user must
    never get a copy, and the count shown in the preview must match."""
    users = UserRepository()
    base = new_user_id()
    before = await users.count_recipients(db_session)
    db_session.add_all(
        [
            User(**build_user_data(base + 3)),
            User(**build_user_data(base + 1)),
            User(**build_user_data(base + 2, is_banned=True)),
            User(**build_user_data(base + 4, is_reachable=False)),
        ]
    )
    await db_session.flush()
    assert await users.get_recipient_ids(db_session, after=base, limit=2) == [
        base + 1,
        base + 3,
    ]
    assert await users.count_recipients(db_session) - before == 2


async def test_users_who_joined_after_the_start_are_not_recipients(
    db_session: AsyncSession,
) -> None:
    """Ids are not monotonic, so a user who starts the bot mid-broadcast may
    land ahead of the cursor; without the cutoff the progress would pass the
    total counted at the start."""
    users = UserRepository()
    base = new_user_id()
    cutoff = get_utc_now()
    before = await users.count_recipients(db_session, registered_by=cutoff)
    db_session.add_all(
        [
            User(**build_user_data(base + 1, created_at=cutoff - timedelta(minutes=1))),
            User(**build_user_data(base + 2, created_at=cutoff + timedelta(seconds=1))),
        ]
    )
    await db_session.flush()
    assert await users.get_recipient_ids(
        db_session, after=base, limit=1, registered_by=cutoff
    ) == [base + 1]
    assert base + 2 not in await users.get_recipient_ids(
        db_session, after=base + 1, limit=1, registered_by=cutoff
    )
    assert await users.count_recipients(db_session, registered_by=cutoff) - before == 1
