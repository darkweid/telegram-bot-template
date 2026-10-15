from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.note.exceptions import NoteNotFoundError
from src.note.models import Note
from src.note.repositories import NoteRepository
from src.note.schemas import NoteView
from src.note.usecases.get_note import GetNoteUseCase
from tests.fakes.db import FakeAsyncSession

NOTE_ID = UUID("01900000-0000-7000-8000-000000000001")


async def test_the_owner_reads_their_note() -> None:
    session, notes = FakeAsyncSession(), AsyncMock(spec=NoteRepository)
    notes.get_single.return_value = Note(id=NOTE_ID, user_id=7, text="Buy milk")
    note = await GetNoteUseCase(SQLAlchemyUnitOfWork(session), notes).execute(
        7, NOTE_ID
    )
    assert note == NoteView(id=NOTE_ID, text="Buy milk")
    notes.get_single.assert_awaited_once_with(session, id=NOTE_ID, user_id=7)


async def test_a_missing_or_foreign_note_is_not_found() -> None:
    session, notes = FakeAsyncSession(), AsyncMock(spec=NoteRepository)
    notes.get_single.return_value = None
    with pytest.raises(NoteNotFoundError):
        await GetNoteUseCase(SQLAlchemyUnitOfWork(session), notes).execute(7, NOTE_ID)
