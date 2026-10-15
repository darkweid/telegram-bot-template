from src.note.exceptions import EmptyNoteError, NoteTooLongError
from src.note.models import NOTE_TEXT_MAX_LENGTH


def check_note_text(text: str) -> None:
    """Refuse a note the `notes` table would refuse, before it gets there.

    Measured in characters, as PostgreSQL's char_length counts them.
    """
    if not text.strip():
        raise EmptyNoteError()
    if len(text) > NOTE_TEXT_MAX_LENGTH:
        raise NoteTooLongError(limit=NOTE_TEXT_MAX_LENGTH, length=len(text))
