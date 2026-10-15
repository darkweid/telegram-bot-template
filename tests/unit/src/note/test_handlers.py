from unittest.mock import AsyncMock
from uuid import UUID

from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    Location,
    Update,
    User,
)
from aiogram.utils.i18n import I18n
from dishka import Provider, Scope, provide

from src.main.config import Config
from src.note.callbacks import NoteCancelCallback, NoteDeleteCallback, NotesPageCallback
from src.note.enums import DeleteAction, PageDirection
from src.note.exceptions import NoteNotFoundError, NoteTooLongError
from src.note.models import NOTE_TEXT_MAX_LENGTH
from src.note.schemas import NotePage, NoteView
from src.note.usecases.create_note import CreateNoteUseCase
from src.note.usecases.delete_note import DeleteNoteUseCase
from src.note.usecases.get_note import GetNoteUseCase
from src.note.usecases.list_notes import ListNotesUseCase
from tests.factories.updates import build_callback_update, build_message_update
from tests.helpers.dispatcher import running_bot
from tests.helpers.user import UseCases

USER = 42  # the factories' default sender
FIRST = UUID("01900000-0000-7000-8000-000000000001")
SECOND = UUID("01900000-0000-7000-8000-000000000002")
PAGE = NotePage(
    notes=(NoteView(id=SECOND, text="<b>bold</b>"), NoteView(id=FIRST, text="a")),
    has_newer=False,
    has_older=True,
)
EMPTY = NotePage(notes=(), has_newer=False, has_older=False)


class NoteStub(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.create = AsyncMock(spec=CreateNoteUseCase)
        self.create.execute.return_value = FIRST
        self.list = AsyncMock(spec=ListNotesUseCase)
        self.list.execute.return_value = PAGE
        self.get = AsyncMock(spec=GetNoteUseCase)
        self.get.execute.return_value = NoteView(id=FIRST, text="Buy milk")
        self.delete = AsyncMock(spec=DeleteNoteUseCase)

    @provide(scope=Scope.REQUEST, override=True)
    def create_note(self) -> CreateNoteUseCase:
        return self.create

    @provide(scope=Scope.REQUEST, override=True)
    def list_notes(self) -> ListNotesUseCase:
        return self.list

    @provide(scope=Scope.REQUEST, override=True)
    def get_note(self) -> GetNoteUseCase:
        return self.get

    @provide(scope=Scope.REQUEST, override=True)
    def delete_note(self) -> DeleteNoteUseCase:
        return self.delete


def _texts(sent: list[SendMessage]) -> list[str]:
    return [message.text for message in sent]


def _location_update() -> Update:
    """A message with no text, the way a shared location or a sticker arrives."""
    update = build_message_update("here")
    assert update.message is not None
    message = update.message.model_copy(
        update={"text": None, "location": Location(latitude=0.0, longitude=0.0)}
    )
    return update.model_copy(update={"message": message})


async def test_a_note_is_saved_from_the_next_message(
    settings: Config, i18n: I18n
) -> None:
    stub = NoteStub()
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/note"))
        await running.feed(build_message_update("Buy milk"))
        await running.feed(build_message_update("just chatting"))
    stub.create.execute.assert_awaited_once_with(USER, "Buy milk")
    prompt, saved = running.session.sent(SendMessage)
    assert prompt.text == (
        f"Send the text of the note, up to {NOTE_TEXT_MAX_LENGTH} characters, "
        "or /cancel."
    )
    assert prompt.reply_markup is not None
    [[cancel]] = prompt.reply_markup.inline_keyboard
    assert cancel.callback_data == NoteCancelCallback().pack()
    assert saved.text == "Note saved. Send /notes to see all of them."


async def test_cancel_command_drops_the_dialog(settings: Config, i18n: I18n) -> None:
    """After /cancel the next message is an ordinary message again, not a note."""
    stub = NoteStub()
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/note"))
        await running.feed(build_message_update("/cancel"))
        await running.feed(build_message_update("Buy milk"))
    stub.create.execute.assert_not_awaited()
    assert _texts(running.session.sent(SendMessage))[-1] == "The note was not saved."


async def test_the_cancel_button_drops_the_dialog(settings: Config, i18n: I18n) -> None:
    stub = NoteStub()
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/note"))
        await running.feed(build_callback_update(NoteCancelCallback().pack()))
        await running.feed(build_message_update("Buy milk"))
    stub.create.execute.assert_not_awaited()
    [edited] = running.session.sent(EditMessageText)
    assert (edited.text, edited.reply_markup) == ("The note was not saved.", None)


async def test_a_stale_cancel_button_changes_nothing(
    settings: Config, i18n: I18n
) -> None:
    """Without an open dialog the button falls through to the unmatched
    router, which only stops the spinner."""
    async with running_bot(settings, i18n, UseCases(), NoteStub()) as running:
        await running.feed(build_callback_update(NoteCancelCallback().pack()))
    assert running.session.sent(EditMessageText) == []
    assert len(running.session.sent(AnswerCallbackQuery)) == 1


async def test_a_command_during_the_dialog_is_not_saved(
    settings: Config, i18n: I18n
) -> None:
    """/help mid-dialog shows help; the dialog stays open for the text."""
    stub = NoteStub()
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/note"))
        await running.feed(build_message_update("/help"))
        await running.feed(build_message_update("Buy milk"))
    stub.create.execute.assert_awaited_once_with(USER, "Buy milk")
    assert _texts(running.session.sent(SendMessage))[1].startswith("Commands:")


async def test_a_message_without_text_is_asked_again(
    settings: Config, i18n: I18n
) -> None:
    stub = NoteStub()
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/note"))
        await running.feed(_location_update())
    stub.create.execute.assert_not_awaited()
    assert _texts(running.session.sent(SendMessage))[-1] == (
        "A note is text only. Send the text, or /cancel."
    )


async def test_a_refused_note_keeps_the_dialog_open(
    settings: Config, i18n: I18n
) -> None:
    """The user shortens the note and sends it again; a closed dialog would
    make them start over and lose nothing but patience."""
    stub = NoteStub()
    stub.create.execute.side_effect = [
        NoteTooLongError(limit=NOTE_TEXT_MAX_LENGTH, length=NOTE_TEXT_MAX_LENGTH + 1),
        FIRST,
    ]
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/note"))
        await running.feed(build_message_update("too long"))
        await running.feed(build_message_update("short"))
    texts = _texts(running.session.sent(SendMessage))
    assert texts[1] == (
        f"A note can be at most {NOTE_TEXT_MAX_LENGTH} characters long. "
        f"This one has {NOTE_TEXT_MAX_LENGTH + 1}."
    )
    assert texts[2] == "Note saved. Send /notes to see all of them."


async def test_notes_shows_the_first_page(settings: Config, i18n: I18n) -> None:
    stub = NoteStub()
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/notes"))
    stub.list.execute.assert_awaited_once_with(USER)
    [reply] = running.session.sent(SendMessage)
    assert reply.text == "Your notes:\n\n1. &lt;b&gt;bold&lt;/b&gt;\n2. a"
    assert reply.reply_markup is not None
    [older] = reply.reply_markup.inline_keyboard[-1]
    assert (
        older.callback_data
        == NotesPageCallback(cursor=FIRST, direction=PageDirection.OLDER).pack()
    )


async def test_no_notes_invites_a_first_one(settings: Config, i18n: I18n) -> None:
    stub = NoteStub()
    stub.list.execute.return_value = EMPTY
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/notes", language_code="ru"))
    [reply] = running.session.sent(SendMessage)
    assert (reply.text, reply.reply_markup) == (
        "Заметок пока нет. Напишите первую: /note.",
        None,
    )


async def test_a_page_button_edits_the_list_in_place(
    settings: Config, i18n: I18n
) -> None:
    stub = NoteStub()
    data = NotesPageCallback(cursor=SECOND, direction=PageDirection.OLDER).pack()
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(build_callback_update(data))
    stub.list.execute.assert_awaited_once_with(USER, SECOND, PageDirection.OLDER)
    [edited] = running.session.sent(EditMessageText)
    assert edited.text == "Your notes:\n\n1. &lt;b&gt;bold&lt;/b&gt;\n2. a"
    assert running.session.sent(SendMessage) == []


async def test_a_page_button_on_an_old_message_asks_for_a_new_list(
    settings: Config, i18n: I18n
) -> None:
    """Telegram no longer lets the bot edit a message older than 48 hours."""
    stub = NoteStub()
    callback = CallbackQuery(
        id="1",
        from_user=User(id=USER, is_bot=False, first_name="Ann", language_code="en"),
        chat_instance="instance",
        message=InaccessibleMessage(
            chat=Chat(id=USER, type="private"), message_id=1, date=0
        ),
        data=NotesPageCallback().pack(),
    )
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(Update(update_id=1, callback_query=callback))
    stub.list.execute.assert_not_awaited()
    [answer] = running.session.sent(AnswerCallbackQuery)
    assert answer.text == "This message is too old. Send /notes again."


async def test_delete_asks_before_it_deletes(settings: Config, i18n: I18n) -> None:
    stub = NoteStub()
    ask = NoteDeleteCallback(note_id=FIRST, action=DeleteAction.ASK).pack()
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(build_callback_update(ask))
    stub.get.execute.assert_awaited_once_with(USER, FIRST)
    stub.delete.execute.assert_not_awaited()
    [prompt] = running.session.sent(EditMessageText)
    assert prompt.text == "Delete this note?\n\nBuy milk"
    assert prompt.reply_markup is not None
    [[confirm, keep]] = prompt.reply_markup.inline_keyboard
    assert (
        confirm.callback_data
        == NoteDeleteCallback(note_id=FIRST, action=DeleteAction.CONFIRM).pack()
    )
    assert keep.callback_data == NotesPageCallback().pack()


async def test_confirming_deletes_and_shows_the_list_again(
    settings: Config, i18n: I18n
) -> None:
    stub = NoteStub()
    confirm = NoteDeleteCallback(note_id=FIRST, action=DeleteAction.CONFIRM).pack()
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(build_callback_update(confirm))
    stub.delete.execute.assert_awaited_once_with(USER, FIRST)
    stub.list.execute.assert_awaited_once_with(USER)
    [answer] = running.session.sent(AnswerCallbackQuery)
    assert answer.text == "Note deleted."
    [page] = running.session.sent(EditMessageText)
    assert page.text == "Your notes:\n\n1. &lt;b&gt;bold&lt;/b&gt;\n2. a"


async def test_a_vanished_note_answers_plainly(settings: Config, i18n: I18n) -> None:
    """The note was deleted from another window, or the button was forged
    with someone else's id: either way the user hears the same thing."""
    stub = NoteStub()
    stub.get.execute.side_effect = NoteNotFoundError()
    ask = NoteDeleteCallback(note_id=FIRST, action=DeleteAction.ASK).pack()
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(build_callback_update(ask))
    assert _texts(running.session.sent(SendMessage)) == ["This note no longer exists."]


async def test_notes_stay_private_to_private_chats(
    settings: Config, i18n: I18n
) -> None:
    """In a group the list would be read by everyone in it."""
    stub = NoteStub()
    async with running_bot(settings, i18n, UseCases(), stub) as running:
        await running.feed(
            build_message_update("/notes", chat_id=-100, chat_type="supergroup")
        )
        await running.feed(
            build_message_update("/note", chat_id=-100, chat_type="supergroup")
        )
    stub.list.execute.assert_not_awaited()
    assert running.session.sent(SendMessage) == []
