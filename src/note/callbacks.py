from uuid import UUID

from aiogram.filters.callback_data import CallbackData

from src.note.enums import DeleteAction, PageDirection


class NoteCancelCallback(CallbackData, prefix="note_cancel"):
    pass


class NotesPageCallback(CallbackData, prefix="notes"):
    """A page of notes read from `cursor`; without one, the first page.

    The cursor travels in the button rather than in the FSM state, so an old
    list keeps paging after the bot restarts.
    """

    cursor: UUID | None = None
    direction: PageDirection = PageDirection.OLDER


class NoteDeleteCallback(CallbackData, prefix="note_del"):
    # Not trusted: the use case deletes only a note of the user who pressed it.
    note_id: UUID
    action: DeleteAction
