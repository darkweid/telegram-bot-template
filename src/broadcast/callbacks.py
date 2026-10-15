from enum import StrEnum
from uuid import UUID

from aiogram.filters.callback_data import CallbackData


class DraftAction(StrEnum):
    SEND = "send"
    CANCEL = "cancel"


class BroadcastDraftCallback(CallbackData, prefix="bdraft"):
    action: DraftAction


class BroadcastStopCallback(CallbackData, prefix="bstop"):
    # "bstop:" plus a 36-character UUID stays under Telegram's 64 bytes.
    broadcast_id: UUID
