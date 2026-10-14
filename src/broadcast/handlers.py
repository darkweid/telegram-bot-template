from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer
from aiogram.utils.i18n import gettext as _
from dishka import FromDishka

from loggers import get_logger
from src.broadcast.callbacks import (
    BroadcastDraftCallback,
    BroadcastStopCallback,
    DraftAction,
)
from src.broadcast.keyboards import confirm_keyboard
from src.broadcast.states import BroadcastStates
from src.broadcast.texts import preview_text
from src.broadcast.usecases.prepare_broadcast import PrepareBroadcastUseCase
from src.broadcast.usecases.start_broadcast import StartBroadcastUseCase
from src.broadcast.usecases.stop_broadcast import StopBroadcastUseCase
from src.core.telegram.messages import safe_edit_text
from src.core.telegram.roles import IsAdmin

logger = get_logger(__name__)


async def broadcast(
    message: Message,
    state: FSMContext,
    prepare_broadcast: FromDishka[PrepareBroadcastUseCase],
) -> None:
    await prepare_broadcast.execute()
    await state.set_state(BroadcastStates.waiting_message)
    await message.answer(
        _(
            "Send the message to broadcast. Every user will get an exact copy of it."
            "\n\nSend /cancel to stop."
        )
    )


async def cancel_draft(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(_("Broadcast cancelled."))


async def reject_album(message: Message) -> None:
    await message.answer(_("Albums are not supported: send a single message."))


async def preview(
    message: Message,
    state: FSMContext,
    prepare_broadcast: FromDishka[PrepareBroadcastUseCase],
) -> None:
    # Counted again: users may have come or gone while the admin wrote.
    count = await prepare_broadcast.execute()
    await message.copy_to(message.chat.id)
    await state.update_data(
        source_chat_id=message.chat.id, source_message_id=message.message_id
    )
    await state.set_state(BroadcastStates.confirming)
    await message.answer(preview_text(count), reply_markup=confirm_keyboard())


async def send(
    callback: CallbackQuery,
    state: FSMContext,
    callback_answer: CallbackAnswer,
    start_broadcast: FromDishka[StartBroadcastUseCase],
) -> None:
    if not isinstance(callback.message, Message):
        # Telegram no longer lets us edit this message, and the progress lives
        # in it.
        callback_answer.text = _(
            "This message is too old. Start again with /broadcast."
        )
        return
    data = await state.get_data()
    # Cleared first: a start refused below leaves no draft behind, and a
    # second tap on Send falls through to the unmatched router.
    await state.clear()
    # Edited before the task is queued: from then on the progress message
    # belongs to the worker, and a later edit here could overwrite its report.
    await safe_edit_text(callback.message, _("Starting the broadcast…"))
    try:
        await start_broadcast.execute(
            admin_id=callback.from_user.id,
            source_chat_id=int(data["source_chat_id"]),
            source_message_id=int(data["source_message_id"]),
            progress_message_id=callback.message.message_id,
        )
    except Exception:
        # Nothing was stored, so no worker will ever edit the message: left
        # alone it would say "Starting" forever. The error goes on to the
        # error handler, which tells the admin why, a refusal included.
        try:
            await safe_edit_text(callback.message, _("The broadcast was not started."))
        except TelegramAPIError as error:
            # The admin may have deleted the message; the original error is
            # the one to report.
            logger.warning(
                "Broadcast progress message was not closed: %s",
                type(error).__name__,
            )
        raise


async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await safe_edit_text(callback.message, _("Broadcast cancelled."))


async def stop(
    callback: CallbackQuery,
    callback_data: BroadcastStopCallback,
    callback_answer: CallbackAnswer,
    stop_broadcast: FromDishka[StopBroadcastUseCase],
) -> None:
    if await stop_broadcast.execute(callback_data.broadcast_id):
        callback_answer.text = _("Stopping the broadcast.")
    else:
        callback_answer.text = _("This broadcast has already finished.")


def build_broadcast_router() -> Router:
    """Admin-only, private chats only; a non-admin's /broadcast falls through
    to the routers below, so the bot does not reveal it exists."""
    router = Router(name="broadcast")
    router.message.filter(F.chat.type == ChatType.PRIVATE, IsAdmin())
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE, IsAdmin())

    router.message.register(broadcast, Command("broadcast"))
    router.message.register(
        cancel_draft, Command("cancel"), StateFilter(BroadcastStates)
    )
    router.message.register(
        reject_album, BroadcastStates.waiting_message, F.media_group_id
    )
    # A command is not a draft: /help while drafting still shows help.
    router.message.register(
        preview, BroadcastStates.waiting_message, ~F.text.startswith("/")
    )
    router.callback_query.register(
        send,
        BroadcastDraftCallback.filter(F.action == DraftAction.SEND),
        BroadcastStates.confirming,
    )
    router.callback_query.register(
        cancel,
        BroadcastDraftCallback.filter(F.action == DraftAction.CANCEL),
        BroadcastStates.confirming,
    )
    router.callback_query.register(stop, BroadcastStopCallback.filter())
    return router
