"""UnitOfWork transaction boundaries against a real transaction.

Commit durability and rollback are properties of PostgreSQL, not of the UoW object:
only a second connection can tell whether the first one's work actually landed.
"""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.user.models import User
from src.user.repositories import UserRepository
from tests.integration.conftest import build_user_data, new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def committed_user_ids(
    integration_engine: AsyncEngine,
) -> AsyncGenerator[list[int]]:
    """Ids of rows a test committed on purpose, removed afterwards.

    The `db_session` fixture's rollback cannot undo these — they are the point of the
    test — so they have to be deleted, or they stay visible to every later test.
    """
    user_ids: list[int] = []
    yield user_ids
    if user_ids:
        async with (
            AsyncSession(integration_engine) as session,
            session.begin(),
        ):
            await session.execute(delete(User).where(User.id.in_(user_ids)))


async def test_commit_is_visible_to_another_session(
    integration_engine: AsyncEngine, committed_user_ids: list[int]
) -> None:
    user_id = new_user_id()
    committed_user_ids.append(user_id)

    async with AsyncSession(integration_engine, expire_on_commit=False) as session:
        uow = SQLAlchemyUnitOfWork(session)
        async with uow:
            await UserRepository().create(uow.session, build_user_data(user_id))
            await uow.flush()
            await uow.commit()

    async with AsyncSession(integration_engine) as other_session:
        persisted = await UserRepository().get_single(other_session, id=user_id)

    assert persisted is not None
    assert persisted.id == user_id


async def test_rollback_leaves_nothing_behind(
    integration_engine: AsyncEngine,
) -> None:
    user_id = new_user_id()

    async with AsyncSession(integration_engine, expire_on_commit=False) as session:
        uow = SQLAlchemyUnitOfWork(session)
        async with uow:
            await UserRepository().create(uow.session, build_user_data(user_id))
            await uow.flush()
            await uow.rollback()

    async with AsyncSession(integration_engine) as other_session:
        persisted = await UserRepository().get_single(other_session, id=user_id)

    assert persisted is None


async def test_repository_commit_inside_a_uow_is_refused(
    db_session: AsyncSession, integration_engine: AsyncEngine
) -> None:
    """The guard has to hold on a real session, where a stray COMMIT is irreversible."""
    uow = SQLAlchemyUnitOfWork(db_session)
    user_id = new_user_id()

    async with uow:
        with pytest.raises(RuntimeError, match="inside an active UnitOfWork"):
            await UserRepository().create(
                uow.session, build_user_data(user_id), commit=True
            )
        # The refusal left the UoW's own transaction usable.
        await uow.commit()

    # Committing above would have made the row durable had the guard let the write
    # through, so its absence is what proves the refusal happened before the INSERT.
    async with AsyncSession(integration_engine) as other_session:
        persisted = await UserRepository().get_single(other_session, id=user_id)

    assert persisted is None
