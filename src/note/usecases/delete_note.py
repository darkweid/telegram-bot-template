from uuid import UUID

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.note.exceptions import NoteNotFoundError
from src.note.repositories import NoteRepository


class DeleteNoteUseCase:
    def __init__(self, uow: SQLAlchemyUnitOfWork, notes: NoteRepository) -> None:
        self._uow = uow
        self._notes = notes

    async def execute(self, user_id: int, note_id: UUID) -> None:
        """Delete the user's own note; another user's is as absent as a
        deleted one."""
        async with self._uow as uow:
            deleted = await self._notes.delete(uow.session, id=note_id, user_id=user_id)
            if deleted is None:
                raise NoteNotFoundError()
            await uow.commit()
