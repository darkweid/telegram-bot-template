from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar

import pytest

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.schemas import Actor, DomainEvent, PublishedEvent
from src.core.outbox.models import OutboxMessage
from src.user.events import UserRegistered
from src.user.subscribers import enqueue_new_user_notice
from src.user.tasks import NOTIFY_ADMINS_OF_NEW_USER
from tests.fakes.db import FakeAsyncSession


@dataclass(frozen=True, slots=True)
class Stranger(DomainEvent):
    """An event no subscriber under test is mapped to."""

    code: ClassVar[str] = "test.stranger"


async def test_a_registration_enqueues_the_admin_notice() -> None:
    session = FakeAsyncSession()
    uow = SQLAlchemyUnitOfWork(session)
    published = PublishedEvent(
        event=UserRegistered(user_id=7),
        actor=Actor.telegram_user(7),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    async with uow:
        await enqueue_new_user_notice(uow, published)
        await uow.commit()
    [row] = [call.args[0] for call in session.add.call_args_list]
    assert isinstance(row, OutboxMessage)
    assert (row.task_name, row.args, row.kwargs) == (
        NOTIFY_ADMINS_OF_NEW_USER,
        [],
        {"user_id": 7},
    )


async def test_an_event_of_another_type_is_refused() -> None:
    """A subscriber mapped to the wrong event must fail loudly, not enqueue."""
    session = FakeAsyncSession()
    uow = SQLAlchemyUnitOfWork(session)
    published = PublishedEvent(
        event=Stranger(),
        actor=Actor.telegram_user(7),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    async with uow:
        with pytest.raises(TypeError, match="Stranger"):
            await enqueue_new_user_notice(uow, published)
    session.add.assert_not_called()
