from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

import pytest

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.registry import event_subscribers, set_event_subscribers
from src.core.events.schemas import Actor, DomainEvent, PublishedEvent
from src.core.outbox.models import OutboxMessage
from tests.fakes.db import FakeAsyncSession


@dataclass(frozen=True, slots=True)
class Pinged(DomainEvent):
    code: ClassVar[str] = "test.pinged"
    target_id: int


class RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[UUID, str, list[Any], dict[str, Any]]] = []

    async def publish(
        self, message_id: UUID, task_name: str, args: list[Any], kwargs: dict[str, Any]
    ) -> None:
        self.published.append((message_id, task_name, args, kwargs))


@pytest.fixture(autouse=True)
def _restore_registry() -> Iterator[None]:
    saved = event_subscribers()
    yield
    set_event_subscribers(saved)


async def test_subscribers_run_inside_the_transaction_of_the_action() -> None:
    """A subscriber writes its own rows (an outbox row above all) in the same
    transaction, so they commit or roll back with the action."""
    seen: list[tuple[bool, PublishedEvent]] = []

    async def subscriber(uow: SQLAlchemyUnitOfWork, published: PublishedEvent) -> None:
        seen.append((uow.session.in_transaction(), published))

    set_event_subscribers({Pinged: [subscriber]})
    uow = SQLAlchemyUnitOfWork(FakeAsyncSession())
    async with uow:
        await uow.publish(Actor.telegram_user(7), Pinged(target_id=1))
        await uow.commit()
    [(in_transaction, published)] = seen
    assert in_transaction
    assert published.event == Pinged(target_id=1)
    assert published.actor == Actor.telegram_user(7)
    assert published.occurred_at.tzinfo is not None


@dataclass(frozen=True, slots=True)
class Ponged(DomainEvent):
    code: ClassVar[str] = "test.ponged"


async def test_an_event_without_subscribers_is_a_no_op() -> None:
    """Subscribers are looked up by the event's exact type: one registered for
    another event must not run, and nothing may be written on its behalf."""
    seen: list[PublishedEvent] = []

    async def subscriber(_: SQLAlchemyUnitOfWork, published: PublishedEvent) -> None:
        seen.append(published)

    set_event_subscribers({Ponged: [subscriber]})
    session = FakeAsyncSession()
    uow = SQLAlchemyUnitOfWork(session)
    async with uow:
        await uow.publish(Actor.system(), Pinged(target_id=1))
        await uow.commit()
    assert seen == []
    session.add.assert_not_called()
    session.execute.assert_not_called()


async def test_publish_outside_the_transaction_is_refused() -> None:
    uow = SQLAlchemyUnitOfWork(FakeAsyncSession())
    with pytest.raises(RuntimeError, match="inside"):
        await uow.publish(Actor.system(), Pinged(target_id=1))


async def test_a_unit_never_entered_is_refused_while_another_is_open() -> None:
    """Units of one update share a session. A unit that was never entered would
    write into the open unit's transaction while its own after-commit hook
    never runs, losing the publish without a trace."""
    session = FakeAsyncSession()
    entered = SQLAlchemyUnitOfWork(session)
    stray = SQLAlchemyUnitOfWork(session, publisher=RecordingPublisher())
    async with entered:
        with pytest.raises(RuntimeError, match="inside"):
            await stray.enqueue_task("notify", user_id=2)
        with pytest.raises(RuntimeError, match="inside"):
            await stray.publish(Actor.system(), Pinged(target_id=1))
        await entered.commit()
    session.add.assert_not_called()


async def test_an_enqueued_task_is_published_after_commit_only() -> None:
    publisher = RecordingPublisher()
    session = FakeAsyncSession()
    uow = SQLAlchemyUnitOfWork(session, publisher=publisher)
    async with uow:
        message_id = await uow.enqueue_task("notify", 1, user_id=2)
        [row] = [call.args[0] for call in session.add.call_args_list]
        assert isinstance(row, OutboxMessage)
        assert (row.id, row.task_name, row.args, row.kwargs) == (
            message_id,
            "notify",
            [1],
            {"user_id": 2},
        )
        assert publisher.published == []
        await uow.commit()
    assert publisher.published == [(message_id, "notify", [1], {"user_id": 2})]


async def test_a_rolled_back_task_is_never_published() -> None:
    publisher = RecordingPublisher()
    uow = SQLAlchemyUnitOfWork(FakeAsyncSession(), publisher=publisher)
    async with uow:
        await uow.enqueue_task("notify", user_id=2)
    assert publisher.published == []


async def test_without_a_publisher_the_row_waits_for_the_sweeper() -> None:
    session = FakeAsyncSession()
    uow = SQLAlchemyUnitOfWork(session)
    async with uow:
        await uow.enqueue_task("notify", user_id=2)
        await uow.commit()
    assert session.add.call_count == 1


async def test_arguments_json_would_change_are_refused_before_any_write() -> None:
    session = FakeAsyncSession()
    uow = SQLAlchemyUnitOfWork(session)
    async with uow:
        with pytest.raises(TypeError):
            await uow.enqueue_task("notify", ids={1: "a"})
    session.add.assert_not_called()
