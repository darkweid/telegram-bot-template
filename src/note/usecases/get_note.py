from uuid import UUID

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.note.exceptions import NoteNotFoundError
from src.note.repositories import NoteRepository
from src.note.schemas import NoteView


class GetNoteUseCase:
    def __init__(self, uow: SQLAlchemyUnitOfWork, notes: NoteRepository) -> None:
        self._uow = uow
        self._notes = notes

    async def execute(self, user_id: int, note_id: UUID) -> NoteView:
        """The user's own note; another user's is as absent as a deleted one."""
        async with self._uow as uow:
            note = await self._notes.get_single(
                uow.session, id=note_id, user_id=user_id
            )
            if note is None:
                raise NoteNotFoundError()
            return NoteView.from_model(note)
