from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.note.callbacks import NoteCancelCallback, NoteDeleteCallback, NotesPageCallback
from src.note.enums import DeleteAction, PageDirection
from src.note.schemas import NotePage

# Delete buttons per row: "Delete 5" still reads whole on a phone.
_DELETE_BUTTONS_PER_ROW = 3


def cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=_("Cancel"), callback_data=NoteCancelCallback())
    return builder.as_markup()


def notes_keyboard(page: NotePage) -> InlineKeyboardMarkup | None:
    """A delete button per note, numbered as in the text, then the page
    buttons; None for an empty page."""
    if not page.notes:
        return None
    builder = InlineKeyboardBuilder()
    for number, note in enumerate(page.notes, start=1):
        builder.button(
            text=_("Delete {number}").format(number=number),
            callback_data=NoteDeleteCallback(note_id=note.id, action=DeleteAction.ASK),
        )
    builder.adjust(_DELETE_BUTTONS_PER_ROW)
    navigation: list[InlineKeyboardButton] = []
    if page.has_newer:
        navigation.append(
            InlineKeyboardButton(
                text=_("« Newer"),
                callback_data=NotesPageCallback(
                    cursor=page.notes[0].id, direction=PageDirection.NEWER
                ).pack(),
            )
        )
    if page.has_older:
        navigation.append(
            InlineKeyboardButton(
                text=_("Older »"),
                callback_data=NotesPageCallback(
                    cursor=page.notes[-1].id, direction=PageDirection.OLDER
                ).pack(),
            )
        )
    if navigation:
        builder.row(*navigation)
    return builder.as_markup()


def confirm_delete_keyboard(note_id: UUID) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=_("Delete"),
        callback_data=NoteDeleteCallback(note_id=note_id, action=DeleteAction.CONFIRM),
    )
    # Back to the first page: the confirmation replaced the page it came
    # from, and the button has no room for that page's cursor too.
    builder.button(text=_("Keep"), callback_data=NotesPageCallback())
    builder.adjust(2)
    return builder.as_markup()
