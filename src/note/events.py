from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID

from src.core.events.schemas import DomainEvent


@dataclass(frozen=True, slots=True)
class NoteCreated(DomainEvent):
    """A note was stored. Carries ids only: a subscriber that needs the text
    reads it, so the text never travels through the outbox or a log."""

    code: ClassVar[str] = "note.created"

    note_id: UUID
    user_id: int
