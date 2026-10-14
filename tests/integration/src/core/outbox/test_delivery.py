import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
import logging
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from redis import exceptions as redis_exceptions
from redis.asyncio import Redis
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from taskiq import InMemoryBroker
from taskiq.message import BrokerMessage

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.outbox import publisher as publisher_module, sweeper as sweeper_module
from src.core.outbox.enums import OutboxMessageStatus
from src.core.outbox.models import OutboxMessage
from src.core.outbox.publisher import OutboxPublisher
from src.core.outbox.repositories import OutboxRepository
from src.core.outbox.sweeper import (
    MAX_PUBLISH_ATTEMPTS,
    PUBLISHED_RETENTION_DAYS,
    purge_outbox,
    sweep_outbox,
)
from src.core.redis.core import create_redis_client
from src.core.tasks.broker import STREAM_QUEUE_NAME, create_stream_broker
from src.core.utils.datetime_utils import get_utc_now
from src.main.config import Config
from tests.helpers.logs import captured_logs
from tests.helpers.redis import full_redis

pytestmark = pytest.mark.asyncio(loop_scope="session")

TASK = "test.deliver"


class Received:
    """Calls of the test task, one instance per test."""

    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []
        calls = self.kwargs

        # taskiq renames the function it registers, which a bound method refuses.
        async def task(**kwargs: Any) -> None:
            calls.append(kwargs)

        self.task = task


class UnreachableBroker(InMemoryBroker):
    """Refuses every send the way a Redis outage does."""

    async def kick(self, message: BrokerMessage) -> None:
        raise redis_exceptions.ConnectionError("broker down")


class HangingBroker(InMemoryBroker):
    """Accepts the connection and never answers, like a blackholed Redis."""

    async def kick(self, message: BrokerMessage) -> None:
        await asyncio.Event().wait()


class RefusingBroker(InMemoryBroker):
    """Refuses every send for a reason that is the message's own."""

    async def kick(self, message: BrokerMessage) -> None:
        raise ValueError("refused")


async def _broker(broker: InMemoryBroker, received: Received) -> InMemoryBroker:
    broker.register_task(received.task, task_name=TASK)
    await broker.startup()
    return broker


@pytest_asyncio.fixture(loop_scope="session")
async def committed_ids(integration_engine: AsyncEngine) -> AsyncGenerator[list[UUID]]:
    ids: list[UUID] = []
    yield ids
    async with AsyncSession(integration_engine) as session, session.begin():
        await session.execute(delete(OutboxMessage).where(OutboxMessage.id.in_(ids)))


async def _enqueue(
    engine: AsyncEngine, ids: list[UUID], broker: InMemoryBroker, **kwargs: Any
) -> UUID:
    publisher = OutboxPublisher(
        broker, async_sessionmaker(engine, expire_on_commit=False)
    )
    async with AsyncSession(engine) as session:
        uow = SQLAlchemyUnitOfWork(session, publisher=publisher)
        async with uow:
            message_id = await uow.enqueue_task(TASK, **kwargs)
            await uow.commit()
    ids.append(message_id)
    return message_id


async def _row(engine: AsyncEngine, message_id: UUID) -> OutboxMessage:
    async with AsyncSession(engine) as session:
        row = await session.get(OutboxMessage, message_id)
    assert row is not None
    return row


async def _age(engine: AsyncEngine, message_id: UUID, **values: Any) -> None:
    """Make a row old enough for the sweeper, or set its counters."""
    async with AsyncSession(engine) as session, session.begin():
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == message_id)
            .values(created_at=get_utc_now() - timedelta(minutes=5), **values)
        )


async def _sweep(engine: AsyncEngine, broker: InMemoryBroker) -> None:
    async with AsyncSession(engine) as session:
        await sweep_outbox(SQLAlchemyUnitOfWork(session), broker)


async def test_a_committed_task_is_sent_and_marked(
    integration_engine: AsyncEngine, committed_ids: list[UUID]
) -> None:
    received = Received()
    broker = await _broker(InMemoryBroker(await_inplace=True), received)
    try:
        message_id = await _enqueue(
            integration_engine, committed_ids, broker, user_id=7
        )
    finally:
        await broker.shutdown()
    assert received.kwargs == [{"user_id": 7}]
    assert (
        await _row(integration_engine, message_id)
    ).status == OutboxMessageStatus.PUBLISHED


async def test_a_failed_after_commit_publish_leaves_the_row_for_the_sweeper(
    integration_engine: AsyncEngine, committed_ids: list[UUID]
) -> None:
    """The user's action is committed; a broker outage at that moment must
    delay the task, not lose it."""
    received = Received()
    down = await _broker(UnreachableBroker(await_inplace=True), received)
    try:
        message_id = await _enqueue(integration_engine, committed_ids, down, user_id=7)
    finally:
        await down.shutdown()
    assert (
        await _row(integration_engine, message_id)
    ).status == OutboxMessageStatus.PENDING

    await _age(integration_engine, message_id)
    up = await _broker(InMemoryBroker(await_inplace=True), received)
    try:
        await _sweep(integration_engine, up)
    finally:
        await up.shutdown()
    assert received.kwargs == [{"user_id": 7}]
    assert (
        await _row(integration_engine, message_id)
    ).status == OutboxMessageStatus.PUBLISHED


async def test_the_sweeper_leaves_a_fresh_row_to_its_own_publish(
    integration_engine: AsyncEngine, committed_ids: list[UUID]
) -> None:
    """A row younger than the grace period may be in its after-commit publish
    right now; taking it would send the task twice."""
    received = Received()
    down = await _broker(UnreachableBroker(await_inplace=True), received)
    try:
        message_id = await _enqueue(integration_engine, committed_ids, down)
    finally:
        await down.shutdown()
    up = await _broker(InMemoryBroker(await_inplace=True), received)
    try:
        await _sweep(integration_engine, up)
    finally:
        await up.shutdown()
    assert received.kwargs == []
    assert (
        await _row(integration_engine, message_id)
    ).status == OutboxMessageStatus.PENDING


async def test_an_unreachable_broker_does_not_count_an_attempt(
    integration_engine: AsyncEngine, committed_ids: list[UUID]
) -> None:
    """Otherwise a ten-minute Redis outage would fail every pending row."""
    received = Received()
    down = await _broker(UnreachableBroker(await_inplace=True), received)
    try:
        message_id = await _enqueue(integration_engine, committed_ids, down)
        await _age(integration_engine, message_id)
        with captured_logs("src.core.outbox.sweeper") as records:
            await _sweep(integration_engine, down)
    finally:
        await down.shutdown()
    row = await _row(integration_engine, message_id)
    assert (row.status, row.attempts) == (OutboxMessageStatus.PENDING, 0)
    assert row.last_error == "ConnectionError"
    # The outage is named once; the summary follows.
    [warning, summary] = records
    assert warning.levelno == logging.WARNING
    assert "ConnectionError" in warning.getMessage()
    assert summary.levelno == logging.INFO


async def test_a_row_refused_too_often_is_failed(
    integration_engine: AsyncEngine, committed_ids: list[UUID]
) -> None:
    received = Received()
    refusing = await _broker(RefusingBroker(await_inplace=True), received)
    try:
        message_id = await _enqueue(integration_engine, committed_ids, refusing)
        await _age(integration_engine, message_id, attempts=MAX_PUBLISH_ATTEMPTS - 1)
        await _sweep(integration_engine, refusing)
    finally:
        await refusing.shutdown()
    row = await _row(integration_engine, message_id)
    assert (row.status, row.attempts, row.last_error) == (
        OutboxMessageStatus.FAILED,
        MAX_PUBLISH_ATTEMPTS,
        "ValueError",
    )


async def test_a_row_of_an_unregistered_task_is_failed_at_once(
    integration_engine: AsyncEngine, committed_ids: list[UUID]
) -> None:
    """A renamed task leaves rows nobody can run; retrying them is noise."""
    received = Received()
    old = await _broker(UnreachableBroker(await_inplace=True), received)
    try:
        message_id = await _enqueue(integration_engine, committed_ids, old)
    finally:
        await old.shutdown()
    await _age(integration_engine, message_id)
    renamed = InMemoryBroker(await_inplace=True)
    await renamed.startup()
    try:
        await _sweep(integration_engine, renamed)
    finally:
        await renamed.shutdown()
    assert (
        await _row(integration_engine, message_id)
    ).status == OutboxMessageStatus.FAILED


async def test_purge_deletes_published_rows_past_retention_in_batches(
    integration_engine: AsyncEngine,
    committed_ids: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One DELETE over a large backlog would run into the statement timeout
    and purge nothing, every night."""
    monkeypatch.setattr(sweeper_module, "PURGE_BATCH_SIZE", 2)
    batches = _spy_purge_batches(monkeypatch)
    received = Received()
    broker = await _broker(InMemoryBroker(await_inplace=True), received)
    try:
        old = [
            await _enqueue(integration_engine, committed_ids, broker) for _ in range(5)
        ]
        recent = await _enqueue(integration_engine, committed_ids, broker)
    finally:
        await broker.shutdown()
    async with AsyncSession(integration_engine) as session, session.begin():
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id.in_(old))
            .values(
                published_at=get_utc_now()
                - timedelta(days=PUBLISHED_RETENTION_DAYS + 1)
            )
        )
    async with AsyncSession(integration_engine) as session:
        await purge_outbox(SQLAlchemyUnitOfWork(session))
    async with AsyncSession(integration_engine) as session:
        for message_id in old:
            assert await session.get(OutboxMessage, message_id) is None
        assert await session.get(OutboxMessage, recent) is not None
    assert len(batches) >= 3
    assert all(size <= 2 for size in batches)
    assert batches[-1] < 2


def _spy_purge_batches(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The size of every DELETE the purge runs."""
    original = OutboxRepository.purge_published
    sizes: list[int] = []

    async def recording(
        self: OutboxRepository, session: AsyncSession, cutoff: datetime, *, limit: int
    ) -> int:
        deleted = await original(self, session, cutoff, limit=limit)
        sizes.append(deleted)
        return deleted

    monkeypatch.setattr(OutboxRepository, "purge_published", recording)
    return sizes


async def test_a_hanging_broker_holds_the_sweep_only_for_the_send_bound(
    integration_engine: AsyncEngine,
    committed_ids: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep holds its batch's row locks while it sends: a blackholed
    Redis must cost one bounded send, and it is no attempt of the row's."""
    monkeypatch.setattr(publisher_module, "SEND_TIMEOUT_SECONDS", 0.1)
    received = Received()
    down = await _broker(UnreachableBroker(await_inplace=True), received)
    try:
        message_id = await _enqueue(integration_engine, committed_ids, down)
    finally:
        await down.shutdown()
    await _age(integration_engine, message_id)
    hanging = await _broker(HangingBroker(await_inplace=True), received)
    try:
        async with asyncio.timeout(5):
            await _sweep(integration_engine, hanging)
    finally:
        await hanging.shutdown()
    row = await _row(integration_engine, message_id)
    assert (row.status, row.attempts, row.last_error) == (
        OutboxMessageStatus.PENDING,
        0,
        "TimeoutError",
    )


async def test_a_full_tasks_redis_does_not_count_an_attempt(
    integration_engine: AsyncEngine,
    committed_ids: list[UUID],
    settings: Config,
    redis_client: Redis,
) -> None:
    """Under noeviction a full Redis refuses XADD with OOM; counted against
    the rows, it would fail the whole backlog in ten sweeps."""
    received = Received()
    down = await _broker(UnreachableBroker(await_inplace=True), received)
    try:
        message_id = await _enqueue(integration_engine, committed_ids, down)
    finally:
        await down.shutdown()
    await _age(integration_engine, message_id)
    stream = create_stream_broker(settings.redis.tasks_dsn)
    stream.register_task(received.task, task_name=TASK)
    # Started before Redis fills up: the start creates the consumer group.
    await stream.startup()
    tasks_redis = create_redis_client(settings.redis.tasks_dsn)
    try:
        async with full_redis(redis_client):
            await _sweep(integration_engine, stream)
    finally:
        await stream.shutdown()
        await tasks_redis.delete(STREAM_QUEUE_NAME)
        await tasks_redis.aclose()
    row = await _row(integration_engine, message_id)
    assert (row.status, row.attempts, row.last_error) == (
        OutboxMessageStatus.PENDING,
        0,
        "OutOfMemoryError",
    )
