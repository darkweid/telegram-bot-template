from collections.abc import AsyncGenerator
from datetime import timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import cast, delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.outbox.enums import OutboxMessageStatus
from src.core.outbox.models import OutboxMessage
from src.core.outbox.payload import JSONB_FLOAT_LIMIT
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


async def test_floats_below_the_limit_come_back_from_jsonb_unchanged(
    integration_engine: AsyncEngine, committed_ids: list[UUID]
) -> None:
    """The payload check accepts these, so the sweeper republishes them from
    the JSONB row: they must arrive as the very floats the inline publish
    sent."""
    floats = [9999999999999998.0, -9999999999999998.0, 1e-07, 0.1 + 0.2]
    async with AsyncSession(integration_engine) as session:
        uow = SQLAlchemyUnitOfWork(session)
        async with uow:
            message_id = await uow.enqueue_task("notify", *floats)
            await uow.commit()
    committed_ids.append(message_id)
    async with AsyncSession(integration_engine) as session:
        row = await session.get(OutboxMessage, message_id)
    assert row is not None
    assert [(type(value), value) for value in row.args] == [
        (float, value) for value in floats
    ]


async def test_jsonb_turns_a_float_at_the_limit_into_an_int(
    integration_engine: AsyncEngine,
) -> None:
    """Why the payload check stops at JSONB_FLOAT_LIMIT: past it the sweeper
    would republish an int where the inline publish sent a float."""
    async with AsyncSession(integration_engine) as session:
        stored = await session.scalar(
            select(cast(literal(f"[{JSONB_FLOAT_LIMIT!r}]"), JSONB))
        )
    assert stored == [10**16]
    assert type(stored[0]) is int


async def test_jsonb_refuses_a_nul_in_a_string(
    integration_engine: AsyncEngine,
) -> None:
    """Why the payload check refuses NUL: the outbox INSERT would fail at
    flush, far from the call that passed the value."""
    async with AsyncSession(integration_engine) as session:
        with pytest.raises(DBAPIError):
            await session.scalar(select(cast(literal('["a\\u0000"]'), JSONB)))


async def test_oldest_pending_age_is_none_without_pending_rows(
    integration_engine: AsyncEngine,
) -> None:
    repository = OutboxRepository()
    async with AsyncSession(integration_engine) as session:
        # Inside a transaction that is rolled back, so rows other tests left
        # pending stay as they were.
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.status == OutboxMessageStatus.PENDING)
            .values(status=OutboxMessageStatus.PUBLISHED)
        )
        assert await repository.oldest_pending_age(session) is None
        await session.rollback()


async def test_oldest_pending_age_measures_the_oldest_row_on_the_database_clock(
    integration_engine: AsyncEngine,
) -> None:
    """/health reports a stalled outbox from this age; a wrong one either
    hides a backlog or raises a false alarm."""
    repository = OutboxRepository()
    backdated_by = timedelta(hours=1)
    async with AsyncSession(integration_engine) as session:
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.status == OutboxMessageStatus.PENDING)
            .values(status=OutboxMessageStatus.PUBLISHED)
        )
        await repository.create(
            session,
            {"task_name": "a", "created_at": func.clock_timestamp() - backdated_by},
        )
        await repository.create(session, {"task_name": "b"})
        await session.flush()
        age = await repository.oldest_pending_age(session)
        await session.rollback()
    assert age is not None
    assert backdated_by <= age < backdated_by + timedelta(minutes=1)
