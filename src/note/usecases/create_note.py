from uuid import UUID

import uuid6

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.note.repositories import NoteRepository
from src.note.services import check_note_text


class CreateNoteUseCase:
    def __init__(self, uow: SQLAlchemyUnitOfWork, notes: NoteRepository) -> None:
        self._uow = uow
        self._notes = notes

    async def execute(self, user_id: int, text: str) -> UUID:
        check_note_text(text)
        # Generated here, not by the column default: the id is known before
        # any flush.
        note_id = uuid6.uuid7()
        async with self._uow as uow:
            await self._notes.create(
                uow.session, {"id": note_id, "user_id": user_id, "text": text}
            )
            await uow.commit()
        return note_id
