import pytest

from src.note.exceptions import EmptyNoteError, NoteTooLongError
from src.note.models import NOTE_TEXT_MAX_LENGTH
from src.note.services import check_note_text


def test_a_note_up_to_the_limit_passes() -> None:
    check_note_text("x" * NOTE_TEXT_MAX_LENGTH)


def test_a_note_past_the_limit_is_refused_before_the_database() -> None:
    """The CHECK constraint would refuse it too, but as an IntegrityError the
    user cannot read; this tells them the limit and their length."""
    with pytest.raises(NoteTooLongError) as raised:
        check_note_text("x" * (NOTE_TEXT_MAX_LENGTH + 1))
    assert raised.value.params == {
        "limit": NOTE_TEXT_MAX_LENGTH,
        "length": NOTE_TEXT_MAX_LENGTH + 1,
    }


def test_a_blank_note_is_refused() -> None:
    with pytest.raises(EmptyNoteError):
        check_note_text(" \n\t")
