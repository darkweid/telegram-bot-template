from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.note.exceptions import NoteNotFoundError
from src.note.models import Note
from src.note.repositories import NoteRepository
from src.note.usecases.delete_note import DeleteNoteUseCase
from tests.fakes.db import FakeAsyncSession

NOTE_ID = UUID("01900000-0000-7000-8000-000000000001")


async def test_the_owner_deletes_their_note() -> None:
    session, notes = FakeAsyncSession(), AsyncMock(spec=NoteRepository)
    notes.delete.return_value = Note(id=NOTE_ID, user_id=7, text="Buy milk")
    await DeleteNoteUseCase(SQLAlchemyUnitOfWork(session), notes).execute(7, NOTE_ID)
    notes.delete.assert_awaited_once_with(session, id=NOTE_ID, user_id=7)
    session.commit.assert_awaited_once()


async def test_a_missing_or_foreign_note_is_not_found() -> None:
    """The owner filter matched nothing: nothing was deleted, and nothing is
    committed."""
    session, notes = FakeAsyncSession(), AsyncMock(spec=NoteRepository)
    notes.delete.return_value = None
    with pytest.raises(NoteNotFoundError):
        await DeleteNoteUseCase(SQLAlchemyUnitOfWork(session), notes).execute(
            7, NOTE_ID
        )
    session.commit.assert_not_awaited()
