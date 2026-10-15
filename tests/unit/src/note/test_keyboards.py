from uuid import UUID

from aiogram.utils.i18n import I18n

from src.note.callbacks import NoteDeleteCallback, NotesPageCallback
from src.note.enums import DeleteAction, PageDirection
from src.note.keyboards import confirm_delete_keyboard, notes_keyboard
from src.note.schemas import NotePage, NoteView

FIRST = UUID("01900000-0000-7000-8000-000000000001")
SECOND = UUID("01900000-0000-7000-8000-000000000002")
THIRD = UUID("01900000-0000-7000-8000-000000000003")


def _page(*, has_newer: bool, has_older: bool) -> NotePage:
    return NotePage(
        notes=(
            NoteView(id=THIRD, text="c"),
            NoteView(id=SECOND, text="b"),
            NoteView(id=FIRST, text="a"),
        ),
        has_newer=has_newer,
        has_older=has_older,
    )


def _rows(page: NotePage, i18n: I18n) -> list[list[tuple[str, str | None]]]:
    with i18n.context(), i18n.use_locale("en"):
        markup = notes_keyboard(page)
    assert markup is not None
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def test_each_note_gets_a_delete_button_by_its_number(i18n: I18n) -> None:
    rows = _rows(_page(has_newer=False, has_older=False), i18n)
    assert rows == [
        [
            (
                "Delete 1",
                NoteDeleteCallback(note_id=THIRD, action=DeleteAction.ASK).pack(),
            ),
            (
                "Delete 2",
                NoteDeleteCallback(note_id=SECOND, action=DeleteAction.ASK).pack(),
            ),
            (
                "Delete 3",
                NoteDeleteCallback(note_id=FIRST, action=DeleteAction.ASK).pack(),
            ),
        ]
    ]


def test_navigation_carries_the_edge_notes_as_cursors(i18n: I18n) -> None:
    """Newer reads before the first note shown, Older past the last one;
    a swapped cursor would skip a page or repeat one."""
    rows = _rows(_page(has_newer=True, has_older=True), i18n)
    assert rows[-1] == [
        (
            "« Newer",
            NotesPageCallback(cursor=THIRD, direction=PageDirection.NEWER).pack(),
        ),
        (
            "Older »",
            NotesPageCallback(cursor=FIRST, direction=PageDirection.OLDER).pack(),
        ),
    ]


def test_an_empty_page_has_no_keyboard(i18n: I18n) -> None:
    with i18n.context(), i18n.use_locale("en"):
        assert (
            notes_keyboard(NotePage(notes=(), has_newer=False, has_older=False)) is None
        )


def test_keep_returns_to_the_first_page(i18n: I18n) -> None:
    with i18n.context(), i18n.use_locale("en"):
        markup = confirm_delete_keyboard(FIRST)
    [[delete, keep]] = markup.inline_keyboard
    assert (delete.text, delete.callback_data) == (
        "Delete",
        NoteDeleteCallback(note_id=FIRST, action=DeleteAction.CONFIRM).pack(),
    )
    assert (keep.text, keep.callback_data) == ("Keep", NotesPageCallback().pack())


def test_page_callbacks_fit_telegrams_limit() -> None:
    """Telegram refuses a button whose callback data exceeds 64 bytes."""
    longest = [
        NotesPageCallback(cursor=FIRST, direction=PageDirection.NEWER).pack(),
        NoteDeleteCallback(note_id=FIRST, action=DeleteAction.CONFIRM).pack(),
    ]
    assert all(len(data.encode()) <= 64 for data in longest)
