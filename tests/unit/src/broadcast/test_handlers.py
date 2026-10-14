from unittest.mock import AsyncMock
from uuid import UUID

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    EditMessageText,
    SendMessage,
)
from aiogram.types import PhotoSize, Update
from aiogram.utils.i18n import I18n
from dishka import Provider, Scope, provide

from src.broadcast.callbacks import (
    BroadcastDraftCallback,
    BroadcastStopCallback,
    DraftAction,
)
from src.broadcast.exceptions import BroadcastInProgressError
from src.broadcast.usecases.prepare_broadcast import PrepareBroadcastUseCase
from src.broadcast.usecases.start_broadcast import StartBroadcastUseCase
from src.broadcast.usecases.stop_broadcast import StopBroadcastUseCase
from src.main.config import Config
from tests.factories.updates import build_callback_update, build_message_update
from tests.helpers.dispatcher import running_bot
from tests.helpers.logs import captured_logs
from tests.helpers.user import UseCases

ADMIN = 1
BROADCAST_ID = UUID("01900000-0000-7000-8000-000000000001")
SEND = BroadcastDraftCallback(action=DraftAction.SEND).pack()
CANCEL = BroadcastDraftCallback(action=DraftAction.CANCEL).pack()


class BroadcastStub(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.prepare = AsyncMock(spec=PrepareBroadcastUseCase)
        self.prepare.execute.return_value = 5
        self.start = AsyncMock(spec=StartBroadcastUseCase)
        self.start.execute.return_value = BROADCAST_ID
        self.stop = AsyncMock(spec=StopBroadcastUseCase)
        self.stop.execute.return_value = True

    @provide(scope=Scope.REQUEST, override=True)
    def prepare_broadcast(self) -> PrepareBroadcastUseCase:
        return self.prepare

    @provide(scope=Scope.REQUEST, override=True)
    def start_broadcast(self) -> StartBroadcastUseCase:
        return self.start

    @provide(scope=Scope.REQUEST, override=True)
    def stop_broadcast(self) -> StopBroadcastUseCase:
        return self.stop


def _photo(media_group_id: str | None = None) -> Update:
    """A captionless photo from the admin; with `media_group_id`, one part of
    an album."""
    update = build_message_update(user_id=ADMIN)
    assert update.message is not None
    photo = PhotoSize(file_id="photo", file_unique_id="photo", width=1, height=1)
    message = update.message.model_copy(
        update={"text": None, "photo": [photo], "media_group_id": media_group_id}
    )
    return update.model_copy(update={"message": message})


def _admin(settings: Config) -> Config:
    return settings.model_copy(
        update={
            "bot": settings.bot.model_copy(update={"ADMIN_IDS": frozenset({ADMIN})})
        }
    )


async def test_an_admin_previews_and_starts_a_broadcast(
    settings: Config, i18n: I18n
) -> None:
    stub = BroadcastStub()
    draft = build_message_update("Hello everyone", user_id=ADMIN)
    send = build_callback_update(SEND, user_id=ADMIN, message_text="preview")
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=ADMIN))
        await running.feed(draft)
        await running.feed(send)
    [preview] = running.session.sent(CopyMessage)
    assert (preview.chat_id, preview.from_chat_id, preview.message_id) == (
        ADMIN,
        ADMIN,
        draft.message.message_id,
    )
    texts = [message.text for message in running.session.sent(SendMessage)]
    assert texts[-1] == "This message will go to 5 users. Send it?"
    stub.start.execute.assert_awaited_once_with(
        admin_id=ADMIN,
        source_chat_id=ADMIN,
        source_message_id=draft.message.message_id,
        progress_message_id=send.callback_query.message.message_id,
    )
    # From here on the message belongs to the worker: an edit after the start
    # could land after the worker's final report and overwrite it.
    [starting] = running.session.sent(EditMessageText)
    assert (starting.text, starting.reply_markup) == ("Starting the broadcast…", None)


async def test_a_start_refused_at_the_last_moment_drops_the_draft(
    settings: Config, i18n: I18n
) -> None:
    """Another admin's broadcast got in between the preview and Send; the
    admin is told why, the message no longer claims a start is under way,
    and the stale draft does not linger in the state."""
    stub = BroadcastStub()
    stub.start.execute.side_effect = BroadcastInProgressError
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=ADMIN))
        await running.feed(build_message_update("Hello", user_id=ADMIN))
        await running.feed(build_callback_update(SEND, user_id=ADMIN))
        await running.feed(build_callback_update(SEND, user_id=ADMIN))
    stub.start.execute.assert_awaited_once()
    assert [edit.text for edit in running.session.sent(EditMessageText)] == [
        "Starting the broadcast…",
        "The broadcast was not started.",
    ]
    assert running.session.sent(SendMessage)[-1].text == (
        "A broadcast is already running. Wait for it to finish or stop it."
    )


async def test_a_failed_start_closes_the_message_and_still_reports_the_error(
    settings: Config, i18n: I18n
) -> None:
    """No worker will edit a message whose broadcast was never stored, and
    the failure must still reach the error handler and its alert."""
    stub = BroadcastStub()
    stub.start.execute.side_effect = RuntimeError("database is down")
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=ADMIN))
        await running.feed(build_message_update("Hello", user_id=ADMIN))
        await running.feed(build_callback_update(SEND, user_id=ADMIN))
    assert running.session.sent(EditMessageText)[-1].text == (
        "The broadcast was not started."
    )
    assert running.session.sent(SendMessage)[-1].text == (
        "Something went wrong. Please try again later."
    )


async def test_a_failed_start_is_reported_even_when_the_message_is_gone(
    settings: Config, i18n: I18n
) -> None:
    """The admin may delete the message meanwhile; the edit failing then must
    not hide the reason the start failed."""
    stub = BroadcastStub()
    stub.start.execute.side_effect = BroadcastInProgressError
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=ADMIN))
        await running.feed(build_message_update("Hello", user_id=ADMIN))
        running.session.queue(EditMessageText, True)
        running.session.queue(
            EditMessageText,
            TelegramBadRequest(
                method=EditMessageText(text="x"),
                message="Bad Request: message to edit not found",
            ),
        )
        with captured_logs("src.broadcast.handlers") as records:
            await running.feed(build_callback_update(SEND, user_id=ADMIN))
    assert [record.getMessage() for record in records] == [
        "Broadcast progress message was not closed: TelegramBadRequest"
    ]
    assert running.session.sent(SendMessage)[-1].text == (
        "A broadcast is already running. Wait for it to finish or stop it."
    )


async def test_the_cancel_button_drops_the_draft(settings: Config, i18n: I18n) -> None:
    stub = BroadcastStub()
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=ADMIN))
        await running.feed(build_message_update("Hello", user_id=ADMIN))
        await running.feed(build_callback_update(CANCEL, user_id=ADMIN))
        await running.feed(build_callback_update(SEND, user_id=ADMIN))
    [cancelled] = running.session.sent(EditMessageText)
    assert cancelled.text == "Broadcast cancelled."
    stub.start.execute.assert_not_awaited()


async def test_cancel_command_leaves_the_draft(settings: Config, i18n: I18n) -> None:
    """After /cancel the next message is an ordinary message again, not a
    broadcast draft."""
    stub = BroadcastStub()
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=ADMIN))
        await running.feed(build_message_update("/cancel", user_id=ADMIN))
        await running.feed(build_message_update("Hello", user_id=ADMIN))
    assert running.session.sent(CopyMessage) == []
    assert running.session.sent(SendMessage)[-1].text == "Broadcast cancelled."


async def test_a_command_during_the_draft_is_not_broadcast(
    settings: Config, i18n: I18n
) -> None:
    stub = BroadcastStub()
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=ADMIN))
        await running.feed(build_message_update("/help", user_id=ADMIN))
    assert running.session.sent(CopyMessage) == []
    assert running.session.sent(SendMessage)[-1].text.startswith("Commands:")


async def test_a_second_broadcast_is_refused_up_front(
    settings: Config, i18n: I18n
) -> None:
    stub = BroadcastStub()
    stub.prepare.execute.side_effect = BroadcastInProgressError
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=ADMIN))
        await running.feed(build_message_update("Hello", user_id=ADMIN))
    assert running.session.sent(CopyMessage) == []
    assert [message.text for message in running.session.sent(SendMessage)] == [
        "A broadcast is already running. Wait for it to finish or stop it."
    ]


async def test_stop_is_acknowledged(settings: Config, i18n: I18n) -> None:
    stub = BroadcastStub()
    stop = BroadcastStopCallback(broadcast_id=BROADCAST_ID).pack()
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_callback_update(stop, user_id=ADMIN))
    stub.stop.execute.assert_awaited_once_with(BROADCAST_ID)
    [answer] = running.session.sent(AnswerCallbackQuery)
    assert answer.text == "Stopping the broadcast."


async def test_broadcast_is_hidden_from_everyone_else(
    settings: Config, i18n: I18n
) -> None:
    stub = BroadcastStub()
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=77))
        await running.feed(build_message_update("Hello", user_id=77))
    stub.prepare.execute.assert_not_awaited()
    assert running.session.sent(CopyMessage) == []


async def test_a_photo_is_a_valid_draft(settings: Config, i18n: I18n) -> None:
    """A broadcast is a copy of any single message, not only of text."""
    stub = BroadcastStub()
    photo = _photo()
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=ADMIN))
        await running.feed(photo)
    [preview] = running.session.sent(CopyMessage)
    assert preview.message_id == photo.message.message_id
    assert running.session.sent(SendMessage)[-1].text == (
        "This message will go to 5 users. Send it?"
    )


async def test_an_album_is_refused_and_the_draft_waits_for_a_single_message(
    settings: Config, i18n: I18n
) -> None:
    """Each part of an album arrives as its own message: copied one by one,
    the broadcast would send only the part that came first."""
    stub = BroadcastStub()
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=ADMIN))
        await running.feed(_photo(media_group_id="album"))
        assert running.session.sent(CopyMessage) == []
        assert running.session.sent(SendMessage)[-1].text == (
            "Albums are not supported: send a single message."
        )
        await running.feed(build_message_update("Hello", user_id=ADMIN))
    assert len(running.session.sent(CopyMessage)) == 1


async def test_an_album_is_refused_once_however_many_parts_it_has(
    settings: Config, i18n: I18n
) -> None:
    """An album of ten photos would otherwise bury the draft under ten
    identical refusals."""
    stub = BroadcastStub()
    refusal = "Albums are not supported: send a single message."
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=ADMIN))
        await running.feed(_photo(media_group_id="first"))
        await running.feed(_photo(media_group_id="first"))
        await running.feed(_photo(media_group_id="second"))
    texts = [sent.text for sent in running.session.sent(SendMessage)]
    assert texts.count(refusal) == 2
    assert running.session.sent(CopyMessage) == []


async def test_stop_on_a_finished_broadcast_says_so(
    settings: Config, i18n: I18n
) -> None:
    """The button stays on the message when the run could not edit it away;
    a tap then must not claim a stop is coming."""
    stub = BroadcastStub()
    stub.stop.execute.return_value = False
    stop = BroadcastStopCallback(broadcast_id=BROADCAST_ID).pack()
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_callback_update(stop, user_id=ADMIN))
    [answer] = running.session.sent(AnswerCallbackQuery)
    assert answer.text == "This broadcast has already finished."


async def test_stop_from_anyone_but_an_admin_is_ignored(
    settings: Config, i18n: I18n
) -> None:
    """An admin who lost the role keeps the old progress message; its button
    must not stop a broadcast any more."""
    stub = BroadcastStub()
    stop = BroadcastStopCallback(broadcast_id=BROADCAST_ID).pack()
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_callback_update(stop, user_id=77))
    stub.stop.execute.assert_not_awaited()
    [answer] = running.session.sent(AnswerCallbackQuery)
    assert answer.text is None
