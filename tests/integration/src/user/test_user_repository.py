import asyncio
from collections.abc import AsyncIterator
import contextlib
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.user.models import User
from src.user.repositories import UserRepository
from src.user.schemas import TelegramProfile
from tests.integration.conftest import new_user_id

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
    return TelegramProfile(**(fields | overrides))  # type: ignore[arg-type]


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
    assert upserted.user.is_blocked is False


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


async def test_blocking_keeps_the_first_block_time_and_upsert_unblocks(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    repository = UserRepository()
    await repository.upsert_from_telegram(db_session, _profile(user_id), T0)
    assert (
        await repository.set_blocked(
            db_session, user_id, True, T0 + timedelta(minutes=1)
        )
        is True
    )
    assert (
        await repository.set_blocked(
            db_session, user_id, True, T0 + timedelta(minutes=2)
        )
        is True
    )
    await db_session.commit()
    blocked = await repository.get_single(
        db_session, id=user_id, populate_existing=True
    )
    assert blocked is not None
    assert (blocked.is_blocked, blocked.blocked_at) == (True, T0 + timedelta(minutes=1))

    upserted = await repository.upsert_from_telegram(
        db_session, _profile(user_id), T0 + timedelta(minutes=3)
    )
    await db_session.commit()
    assert (upserted.user.is_blocked, upserted.user.blocked_at) == (False, None)


async def test_unblocking_clears_the_block_time(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    repository = UserRepository()
    await repository.upsert_from_telegram(db_session, _profile(user_id), T0)
    await repository.set_blocked(db_session, user_id, True, T0)
    await repository.set_blocked(db_session, user_id, False, T0)
    await db_session.commit()
    user = await repository.get_single(db_session, id=user_id, populate_existing=True)
    assert user is not None
    assert (user.is_blocked, user.blocked_at) == (False, None)


async def test_writes_to_an_unknown_user_report_false(
    db_session: AsyncSession, user_id: int
) -> None:
    repository = UserRepository()
    assert await repository.set_blocked(db_session, user_id, True, T0) is False
    assert await repository.set_locale(db_session, user_id, "en") is False
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


async def test_set_blocked_and_set_locale_move_updated_at(
    db_session: AsyncSession, user_id: int, cleanup: None
) -> None:
    repository = UserRepository()
    created = (
        await repository.upsert_from_telegram(db_session, _profile(user_id), T0)
    ).user
    await db_session.commit()
    stamps = [created.updated_at]

    for write in (
        lambda: repository.set_blocked(db_session, user_id, True, T0),
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
