from datetime import UTC, datetime
from uuid import UUID

import pytest

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.schemas import Actor, PublishedEvent
from src.core.outbox.models import OutboxMessage
from src.note.events import NoteCreated
from src.note.subscribers import enqueue_note_created
from src.note.tasks import NOTE_CREATED
from src.user.events import UserRegistered
from tests.fakes.db import FakeAsyncSession

NOTE_ID = UUID("01900000-0000-7000-8000-000000000001")


async def test_a_new_note_enqueues_its_task() -> None:
    session = FakeAsyncSession()
    uow = SQLAlchemyUnitOfWork(session)
    published = PublishedEvent(
        event=NoteCreated(note_id=NOTE_ID, user_id=7),
        actor=Actor.telegram_user(7),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    async with uow:
        await enqueue_note_created(uow, published)
        await uow.commit()
    [row] = [call.args[0] for call in session.add.call_args_list]
    assert isinstance(row, OutboxMessage)
    # A string: the arguments travel as JSON, which has no UUID.
    assert (row.task_name, row.args, row.kwargs) == (
        NOTE_CREATED,
        [],
        {"note_id": str(NOTE_ID), "user_id": 7},
    )


async def test_an_event_of_another_type_is_refused() -> None:
    """A subscriber mapped to the wrong event must fail loudly, not enqueue."""
    session = FakeAsyncSession()
    uow = SQLAlchemyUnitOfWork(session)
    published = PublishedEvent(
        event=UserRegistered(user_id=7),
        actor=Actor.telegram_user(7),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    async with uow:
        with pytest.raises(TypeError, match="UserRegistered"):
            await enqueue_note_created(uow, published)
    session.add.assert_not_called()
