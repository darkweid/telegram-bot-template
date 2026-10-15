from functools import partial

from aiogram import Bot, F, Router, html
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer
from aiogram.utils.i18n import I18n, gettext as _, ngettext
from dishka import AsyncContainer, FromDishka

from src.core.telegram.background import BackgroundRunner
from src.core.telegram.messages import safe_edit_text
from src.core.telegram.middlewares.throttling import Throttle
from src.core.telegram.status import ProcessingStatus
from src.note.callbacks import NoteCancelCallback, NoteDeleteCallback, NotesPageCallback
from src.note.enums import DeleteAction
from src.note.exceptions import NoteNotFoundError
from src.note.keyboards import cancel_keyboard, confirm_delete_keyboard, notes_keyboard
from src.note.models import NOTE_TEXT_MAX_LENGTH
from src.note.states import NoteStates
from src.note.texts import delete_prompt, page_text
from src.note.usecases.create_note import CreateNoteUseCase
from src.note.usecases.delete_note import DeleteNoteUseCase
from src.note.usecases.get_note import GetNoteUseCase
from src.note.usecases.list_notes import ListNotesUseCase
from src.note.usecases.summarize_notes import NoteSummaryResult, SummarizeNotesUseCase
from src.user.schemas import UserProfile


async def new_note(message: Message, state: FSMContext) -> None:
    await state.set_state(NoteStates.waiting_text)
    await message.answer(
        _("Send the text of the note, up to {limit} characters, or /cancel.").format(
            limit=NOTE_TEXT_MAX_LENGTH
        ),
        reply_markup=cancel_keyboard(),
    )


async def cancel_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(_("The note was not saved."))


async def cancel_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        # Edited without a markup: the Cancel button goes with the prompt.
        await safe_edit_text(callback.message, _("The note was not saved."))


async def save_note(
    message: Message,
    text: str,
    state: FSMContext,
    user: UserProfile,
    create_note: FromDishka[CreateNoteUseCase],
) -> None:
    await create_note.execute(user.id, text)
    # Cleared only once saved: a refused note leaves the dialog open for a
    # shorter one.
    await state.clear()
    await message.answer(_("Note saved. Send /notes to see all of them."))


async def ask_for_text(message: Message) -> None:
    await message.answer(_("A note is text only. Send the text, or /cancel."))


async def show_notes(
    message: Message, user: UserProfile, list_notes: FromDishka[ListNotesUseCase]
) -> None:
    page = await list_notes.execute(user.id)
    await message.answer(page_text(page), reply_markup=notes_keyboard(page))


async def turn_page(
    callback: CallbackQuery,
    callback_data: NotesPageCallback,
    callback_answer: CallbackAnswer,
    user: UserProfile,
    list_notes: FromDishka[ListNotesUseCase],
) -> None:
    if not isinstance(callback.message, Message):
        callback_answer.text = _("This message is too old. Send /notes again.")
        return
    page = await list_notes.execute(
        user.id, callback_data.cursor, callback_data.direction
    )
    await safe_edit_text(
        callback.message, page_text(page), reply_markup=notes_keyboard(page)
    )


async def ask_to_delete(
    callback: CallbackQuery,
    callback_data: NoteDeleteCallback,
    callback_answer: CallbackAnswer,
    user: UserProfile,
    get_note: FromDishka[GetNoteUseCase],
) -> None:
    if not isinstance(callback.message, Message):
        callback_answer.text = _("This message is too old. Send /notes again.")
        return
    note = await get_note.execute(user.id, callback_data.note_id)
    await safe_edit_text(
        callback.message,
        delete_prompt(note),
        reply_markup=confirm_delete_keyboard(note.id),
    )


async def confirm_delete(
    callback: CallbackQuery,
    callback_data: NoteDeleteCallback,
    callback_answer: CallbackAnswer,
    user: UserProfile,
    delete_note: FromDishka[DeleteNoteUseCase],
    list_notes: FromDishka[ListNotesUseCase],
) -> None:
    try:
        await delete_note.execute(user.id, callback_data.note_id)
    except NoteNotFoundError:
        # A double tap, or a note deleted from another window: the list is
        # shown again below, so the stale confirmation goes away.
        callback_answer.text = _("This note no longer exists.")
    else:
        callback_answer.text = _("Note deleted.")
    if not isinstance(callback.message, Message):
        return
    page = await list_notes.execute(user.id)
    await safe_edit_text(
        callback.message, page_text(page), reply_markup=notes_keyboard(page)
    )


async def summarize(
    message: Message,
    bot: Bot,
    i18n: I18n,
    user: UserProfile,
    background: FromDishka[BackgroundRunner],
) -> None:
    # The model takes seconds: the handler lets go of the update at once and
    # the work edits the status into the answer.
    status = await ProcessingStatus.send(
        bot, message.chat.id, i18n=i18n, text=_("Summarizing your notes…")
    )
    background.start(
        "note.summarize",
        partial(
            summarize_in_background,
            status=status,
            user_id=user.id,
            locale=i18n.current_locale,
        ),
    )


async def summarize_in_background(
    container: AsyncContainer, *, status: ProcessingStatus, user_id: int, locale: str
) -> None:
    """The model is called inside the scope but after the use case's unit of
    work: no pooled connection waits on it."""
    async with status:
        async with container() as scope:
            summarize_notes = await scope.get(SummarizeNotesUseCase)
            result = await summarize_notes.execute(user_id, locale)
        if result is None:
            # The msgid of the empty /notes list: one text, one translation.
            await status.finish(_("You have no notes yet. Send /note to write one."))
            return
        await status.finish(_summary_text(result))


def _summary_text(result: NoteSummaryResult) -> str:
    """The model's output comes from what the user typed, so every part of
    it is escaped for HTML parse mode."""
    header = ngettext(
        "Summary of {count} recent note:",
        "Summary of {count} recent notes:",
        result.note_count,
    ).format(count=result.note_count)
    parts = [header, html.quote(result.summary.summary)]
    if result.summary.themes:
        parts.append(
            "\n".join(f"• {html.quote(theme)}" for theme in result.summary.themes)
        )
    return "\n\n".join(parts)


def build_note_router() -> Router:
    """A new router per dispatcher: aiogram attaches a router to one parent
    only, and tests build a dispatcher each."""
    router = Router(name="note")
    # Private chats only: in a group everyone would read the notes.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    router.message.register(new_note, Command("note"))
    router.message.register(show_notes, Command("notes"))
    router.message.register(cancel_command, Command("cancel"), StateFilter(NoteStates))
    # A command is not a note: /help mid-dialog still shows help, and the
    # dialog waits for the text.
    router.message.register(
        save_note,
        NoteStates.waiting_text,
        F.text.as_("text"),
        ~F.text.startswith("/"),
    )
    router.message.register(ask_for_text, NoteStates.waiting_text, ~F.text)
    router.callback_query.register(
        cancel_button, NoteCancelCallback.filter(), StateFilter(NoteStates)
    )
    router.callback_query.register(turn_page, NotesPageCallback.filter())
    router.callback_query.register(
        ask_to_delete, NoteDeleteCallback.filter(F.action == DeleteAction.ASK)
    )
    router.callback_query.register(
        confirm_delete, NoteDeleteCallback.filter(F.action == DeleteAction.CONFIRM)
    )
    router.message.register(
        summarize, Command("summarize"), flags={"throttle": Throttle.LLM}
    )
    return router
