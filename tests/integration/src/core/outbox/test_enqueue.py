from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.outbox.enums import OutboxMessageStatus
from src.core.outbox.models import OutboxMessage
from src.core.outbox.repositories import OutboxRepository

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def committed_ids(integration_engine: AsyncEngine) -> AsyncGenerator[list[UUID]]:
    ids: list[UUID] = []
    yield ids
    async with AsyncSession(integration_engine) as session, session.begin():
        await session.execute(delete(OutboxMessage).where(OutboxMessage.id.in_(ids)))


async def test_a_committed_task_is_a_pending_row(
    integration_engine: AsyncEngine, committed_ids: list[UUID]
) -> None:
    async with AsyncSession(integration_engine) as session:
        uow = SQLAlchemyUnitOfWork(session)
        async with uow:
            message_id = await uow.enqueue_task("notify", 1, user_id=2)
            await uow.commit()
    committed_ids.append(message_id)
    async with AsyncSession(integration_engine) as session:
        row = await session.get(OutboxMessage, message_id)
    assert row is not None
    assert (row.task_name, row.args, row.kwargs, row.status) == (
        "notify",
        [1],
        {"user_id": 2},
        OutboxMessageStatus.PENDING,
    )


async def test_a_rolled_back_task_leaves_no_row(
    integration_engine: AsyncEngine,
) -> None:
    async with AsyncSession(integration_engine) as session:
        uow = SQLAlchemyUnitOfWork(session)
        async with uow:
            message_id = await uow.enqueue_task("notify", user_id=2)
    async with AsyncSession(integration_engine) as session:
        assert await session.get(OutboxMessage, message_id) is None


async def test_backlog_metrics_see_only_pending_rows(
    integration_engine: AsyncEngine, committed_ids: list[UUID]
) -> None:
    repository = OutboxRepository()
    async with AsyncSession(integration_engine) as session:
        before = await repository.count_pending(session)
    async with AsyncSession(integration_engine) as session:
        uow = SQLAlchemyUnitOfWork(session)
        async with uow:
            pending = await uow.enqueue_task("a")
            done = await uow.enqueue_task("b")
            await uow.commit()
    committed_ids.extend([pending, done])
    async with AsyncSession(integration_engine) as session, session.begin():
        await repository.mark_published(session, done)
    async with AsyncSession(integration_engine) as session:
        assert await repository.count_pending(session) == before + 1
        age = await repository.oldest_pending_age(session)
    assert age is not None and age.total_seconds() >= 0
