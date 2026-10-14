from unittest.mock import AsyncMock
from uuid import UUID

from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    EditMessageText,
    SendMessage,
)
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
    admin is told why, and the stale draft does not linger in the state."""
    stub = BroadcastStub()
    stub.start.execute.side_effect = BroadcastInProgressError
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/broadcast", user_id=ADMIN))
        await running.feed(build_message_update("Hello", user_id=ADMIN))
        await running.feed(build_callback_update(SEND, user_id=ADMIN))
        await running.feed(build_callback_update(SEND, user_id=ADMIN))
    stub.start.execute.assert_awaited_once()
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
