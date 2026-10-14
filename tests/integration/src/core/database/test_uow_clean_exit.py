from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.user.models import User
from src.user.repositories import UserRepository
from tests.integration.conftest import build_user_data, new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _find(session: AsyncSession, user_id: int) -> User | None:
    return await session.scalar(select(User).where(User.id == user_id))


async def test_clean_exit_without_commit_persists_nothing_on_fresh_session(
    db_session: AsyncSession,
) -> None:
    user_id = new_user_id()
    uow = SQLAlchemyUnitOfWork(db_session)
    async with uow:
        await UserRepository().create(uow.session, data=build_user_data(user_id))

    assert await _find(db_session, user_id) is None


async def test_entering_after_a_raw_read_raises(db_session: AsyncSession) -> None:
    """A plain SELECT on the shared session autobegins a transaction; the UoW
    used to nest a SAVEPOINT in it and commit it on `commit()`."""
    await db_session.execute(select(1))

    with pytest.raises(RuntimeError, match="already in a transaction"):
        async with SQLAlchemyUnitOfWork(db_session):
            pass

    assert db_session.in_transaction()
    assert "uow_active" not in db_session.info


async def test_entering_after_staged_work_raises_and_leaves_the_session_as_it_was(
    db_session: AsyncSession,
) -> None:
    """The refusal must leave the caller's staged work and transaction
    untouched: no rollback, no marker left behind."""
    user_id = new_user_id()
    await UserRepository().create(db_session, data=build_user_data(user_id))
    await db_session.flush()

    with pytest.raises(RuntimeError, match="already in a transaction"):
        async with SQLAlchemyUnitOfWork(db_session):
            pass

    assert db_session.in_transaction()
    assert "uow_active" not in db_session.info
    assert await _find(db_session, user_id) is not None


async def test_a_unit_of_work_inside_a_unit_of_work_raises(
    db_session: AsyncSession,
) -> None:
    """The inner exit used to pop `uow_active` and lift the outer UoW's guard
    against a repository `commit=True`."""
    user_id = new_user_id()
    repository = UserRepository()
    outer = SQLAlchemyUnitOfWork(db_session)
    async with outer:
        await repository.create(outer.session, data=build_user_data(user_id))
        with pytest.raises(RuntimeError, match="already in a transaction"):
            async with SQLAlchemyUnitOfWork(db_session):
                pass
        assert db_session.info.get("uow_active") is True
        with pytest.raises(RuntimeError, match="inside an active UnitOfWork"):
            await repository.create(
                outer.session, data=build_user_data(new_user_id()), commit=True
            )

    assert await _find(db_session, user_id) is None


async def test_sequential_units_of_work_on_one_session(
    db_session: AsyncSession,
) -> None:
    """The guard must not fire on a session the previous unit already ended."""
    first_id = new_user_id()
    second_id = new_user_id()
    repository = UserRepository()

    first = SQLAlchemyUnitOfWork(db_session)
    async with first:
        await repository.create(first.session, data=build_user_data(first_id))
        await first.rollback()
    assert not db_session.in_transaction()

    second = SQLAlchemyUnitOfWork(db_session)
    async with second:
        await repository.create(second.session, data=build_user_data(second_id))
        await second.flush()
        found = await repository.get_single(second.session, id=second_id)
        assert found is not None

    assert not db_session.in_transaction()
    assert await _find(db_session, second_id) is None
