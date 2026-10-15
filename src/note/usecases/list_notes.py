from uuid import UUID

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.note.enums import PageDirection
from src.note.repositories import NoteRepository
from src.note.schemas import NotePage, NoteView

# Five previews and a row of delete buttons fit one phone screen.
NOTES_PAGE_SIZE = 5


class ListNotesUseCase:
    def __init__(self, uow: SQLAlchemyUnitOfWork, notes: NoteRepository) -> None:
        self._uow = uow
        self._notes = notes

    async def execute(
        self,
        user_id: int,
        cursor: UUID | None = None,
        direction: PageDirection = PageDirection.OLDER,
    ) -> NotePage:
        async with self._uow as uow:
            rows = await self._notes.page_for_user(
                uow.session,
                user_id,
                cursor=cursor,
                direction=direction,
                size=NOTES_PAGE_SIZE,
            )
            if not rows.notes and cursor is not None:
                # Every note past the cursor was deleted since the page was
                # shown: start over rather than show an empty page.
                rows = await self._notes.page_for_user(
                    uow.session,
                    user_id,
                    cursor=None,
                    direction=PageDirection.OLDER,
                    size=NOTES_PAGE_SIZE,
                )
            # Inside the block: leaving it without commit rolls back and
            # expires the rows.
            return NotePage(
                notes=tuple(NoteView.from_model(note) for note in rows.notes),
                has_newer=rows.has_newer,
                has_older=rows.has_older,
            )
