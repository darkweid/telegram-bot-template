from dataclasses import dataclass
from typing import Self
from uuid import UUID

from src.note.models import Note


@dataclass(frozen=True, slots=True)
class NoteView:
    """A stored note as handlers see it: a snapshot, detached from any session."""

    id: UUID
    text: str

    @classmethod
    def from_model(cls, note: Note) -> Self:
        return cls(id=note.id, text=note.text)


@dataclass(frozen=True, slots=True)
class NotePage:
    """One page of a user's notes, newest first, and whether pages lie
    beyond it either way."""

    notes: tuple[NoteView, ...]
    has_newer: bool
    has_older: bool
