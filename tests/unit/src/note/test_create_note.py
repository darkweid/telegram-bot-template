from collections.abc import Iterator
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.registry import event_subscribers, set_event_subscribers
from src.core.events.schemas import Actor, PublishedEvent
from src.note.events import NoteCreated
from src.note.exceptions import EmptyNoteError, NoteTooLongError
from src.note.models import NOTE_TEXT_MAX_LENGTH
from src.note.repositories import NoteRepository
from src.note.usecases.create_note import CreateNoteUseCase
from tests.fakes.db import FakeAsyncSession


@pytest.fixture
def published() -> Iterator[list[PublishedEvent]]:
    """Record what the use case publishes, then put the real registry back."""
    seen: list[PublishedEvent] = []

    async def record(_uow: SQLAlchemyUnitOfWork, event: PublishedEvent) -> None:
        seen.append(event)

    saved = event_subscribers()
    set_event_subscribers({NoteCreated: [record]})
    yield seen
    set_event_subscribers(saved)


def _use_case(session: FakeAsyncSession, notes: AsyncMock) -> CreateNoteUseCase:
    return CreateNoteUseCase(SQLAlchemyUnitOfWork(session), notes)


async def test_a_note_is_stored_and_its_id_returned() -> None:
    session, notes = FakeAsyncSession(), AsyncMock(spec=NoteRepository)
    note_id = await _use_case(session, notes).execute(7, "Buy milk")
    assert isinstance(note_id, UUID) and note_id.version == 7
    notes.create.assert_awaited_once_with(
        session, {"id": note_id, "user_id": 7, "text": "Buy milk"}
    )
    session.commit.assert_awaited_once()


async def test_a_refused_note_never_reaches_the_database() -> None:
    session, notes = FakeAsyncSession(), AsyncMock(spec=NoteRepository)
    with pytest.raises(NoteTooLongError):
        await _use_case(session, notes).execute(7, "x" * (NOTE_TEXT_MAX_LENGTH + 1))
    notes.create.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_a_stored_note_publishes_note_created(
    published: list[PublishedEvent],
) -> None:
    session, notes = FakeAsyncSession(), AsyncMock(spec=NoteRepository)
    note_id = await _use_case(session, notes).execute(7, "Buy milk")
    [event] = published
    assert event.event == NoteCreated(note_id=note_id, user_id=7)
    assert event.actor == Actor.telegram_user(7)


async def test_a_refused_note_publishes_nothing(
    published: list[PublishedEvent],
) -> None:
    """Subscribers act on a note that exists; a refused one never did."""
    session, notes = FakeAsyncSession(), AsyncMock(spec=NoteRepository)
    with pytest.raises(EmptyNoteError):
        await _use_case(session, notes).execute(7, "   ")
    assert published == []
