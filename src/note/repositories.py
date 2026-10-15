from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database.repositories import BaseRepository
from src.note.enums import PageDirection
from src.note.models import Note


@dataclass(frozen=True, slots=True)
class NoteRows:
    """One page of notes, newest first, and whether pages lie beyond it."""

    notes: list[Note]
    has_newer: bool
    has_older: bool


class NoteRepository(BaseRepository[Note]):
    model = Note

    async def page_for_user(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        cursor: UUID | None,
        direction: PageDirection,
        size: int,
    ) -> NoteRows:
        """A page of the user's notes, keyset by id.

        uuid7 ids grow with creation time and are unique, so `id` alone
        orders the notes with no tie-breaker, and the (user_id, id) index
        serves every page with a range scan however deep it is. OLDER reads
        past `cursor` (the last note shown), NEWER before it (the first one
        shown). Without a cursor OLDER starts at the newest note.
        """
        if size < 1:
            raise ValueError("size must be at least 1")
        newer = direction is PageDirection.NEWER
        query = select(Note).where(Note.user_id == user_id)
        if cursor is not None:
            query = query.where(Note.id > cursor if newer else Note.id < cursor)
        query = query.order_by(Note.id.asc() if newer else Note.id.desc())
        # One row past the page tells whether another page follows this way.
        rows = list(await session.scalars(query.limit(size + 1)))
        further = len(rows) > size
        notes = rows[:size]
        if newer:
            notes.reverse()
        if not notes:
            return NoteRows(notes=[], has_newer=False, has_older=False)
        if newer:
            return NoteRows(
                notes=notes,
                has_newer=further,
                has_older=await self._any(session, user_id, Note.id < notes[-1].id),
            )
        return NoteRows(
            notes=notes,
            has_newer=await self._any(session, user_id, Note.id > notes[0].id),
            has_older=further,
        )

    async def latest_for_user(
        self, session: AsyncSession, user_id: int, limit: int
    ) -> list[Note]:
        """The user's `limit` newest notes, newest first."""
        query = (
            select(Note)
            .where(Note.user_id == user_id)
            .order_by(Note.id.desc())
            .limit(limit)
        )
        return list(await session.scalars(query))

    async def _any(
        self, session: AsyncSession, user_id: int, condition: ColumnElement[bool]
    ) -> bool:
        query = select(
            select(Note.id).where(Note.user_id == user_id, condition).exists()
        )
        return bool(await session.scalar(query))
