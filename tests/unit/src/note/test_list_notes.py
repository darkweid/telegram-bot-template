from unittest.mock import AsyncMock, call
from uuid import UUID

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.note.enums import PageDirection
from src.note.models import Note
from src.note.repositories import NoteRepository, NoteRows
from src.note.schemas import NotePage, NoteView
from src.note.usecases.list_notes import NOTES_PAGE_SIZE, ListNotesUseCase
from tests.fakes.db import FakeAsyncSession

FIRST = UUID("01900000-0000-7000-8000-000000000001")
SECOND = UUID("01900000-0000-7000-8000-000000000002")


def _note(note_id: UUID, text: str) -> Note:
    return Note(id=note_id, user_id=7, text=text)


async def test_the_first_page_is_read_from_the_newest_note() -> None:
    session, notes = FakeAsyncSession(), AsyncMock(spec=NoteRepository)
    notes.page_for_user.return_value = NoteRows(
        notes=[_note(SECOND, "b"), _note(FIRST, "a")], has_newer=False, has_older=True
    )
    page = await ListNotesUseCase(SQLAlchemyUnitOfWork(session), notes).execute(7)
    assert page == NotePage(
        notes=(NoteView(id=SECOND, text="b"), NoteView(id=FIRST, text="a")),
        has_newer=False,
        has_older=True,
    )
    notes.page_for_user.assert_awaited_once_with(
        session, 7, cursor=None, direction=PageDirection.OLDER, size=NOTES_PAGE_SIZE
    )


async def test_a_page_whose_notes_are_gone_falls_back_to_the_first() -> None:
    """Every note past the cursor was deleted since the page was shown (in
    another chat window); an empty page without buttons would strand the user."""
    session, notes = FakeAsyncSession(), AsyncMock(spec=NoteRepository)
    first_page = NoteRows(notes=[_note(FIRST, "a")], has_newer=False, has_older=False)
    notes.page_for_user.side_effect = [
        NoteRows(notes=[], has_newer=False, has_older=False),
        first_page,
    ]
    page = await ListNotesUseCase(SQLAlchemyUnitOfWork(session), notes).execute(
        7, SECOND, PageDirection.OLDER
    )
    assert page.notes == (NoteView(id=FIRST, text="a"),)
    assert notes.page_for_user.await_args_list == [
        call(
            session,
            7,
            cursor=SECOND,
            direction=PageDirection.OLDER,
            size=NOTES_PAGE_SIZE,
        ),
        call(
            session, 7, cursor=None, direction=PageDirection.OLDER, size=NOTES_PAGE_SIZE
        ),
    ]


async def test_a_newer_page_whose_notes_are_gone_falls_back_to_the_first() -> None:
    """The same stranded user, arriving through the Newer button."""
    session, notes = FakeAsyncSession(), AsyncMock(spec=NoteRepository)
    notes.page_for_user.side_effect = [
        NoteRows(notes=[], has_newer=False, has_older=False),
        NoteRows(notes=[_note(FIRST, "a")], has_newer=False, has_older=False),
    ]
    page = await ListNotesUseCase(SQLAlchemyUnitOfWork(session), notes).execute(
        7, SECOND, PageDirection.NEWER
    )
    assert page.notes == (NoteView(id=FIRST, text="a"),)
    assert notes.page_for_user.await_args_list[1] == call(
        session, 7, cursor=None, direction=PageDirection.OLDER, size=NOTES_PAGE_SIZE
    )


async def test_listing_writes_nothing() -> None:
    session, notes = FakeAsyncSession(), AsyncMock(spec=NoteRepository)
    notes.page_for_user.return_value = NoteRows(
        notes=[], has_newer=False, has_older=False
    )
    await ListNotesUseCase(SQLAlchemyUnitOfWork(session), notes).execute(7)
    session.commit.assert_not_awaited()
