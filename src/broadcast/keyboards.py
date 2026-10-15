from uuid import UUID

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.broadcast.callbacks import (
    BroadcastDraftCallback,
    BroadcastStopCallback,
    DraftAction,
)


def confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=_("Send"), callback_data=BroadcastDraftCallback(action=DraftAction.SEND)
    )
    builder.button(
        text=_("Cancel"),
        callback_data=BroadcastDraftCallback(action=DraftAction.CANCEL),
    )
    builder.adjust(2)
    return builder.as_markup()


def stop_keyboard(broadcast_id: UUID) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=_("Stop"), callback_data=BroadcastStopCallback(broadcast_id=broadcast_id)
    )
    return builder.as_markup()
