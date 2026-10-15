from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.note.exceptions import NoteTooLongError
from src.note.models import NOTE_TEXT_MAX_LENGTH
from src.note.repositories import NoteRepository
from src.note.usecases.create_note import CreateNoteUseCase
from tests.fakes.db import FakeAsyncSession


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
