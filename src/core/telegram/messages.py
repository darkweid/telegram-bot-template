from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

_NOT_MODIFIED = "message is not modified"


async def safe_edit_text(
    message: Message, text: str, **kwargs: Any
) -> Message | bool | None:
    """Edit a message, treating "nothing changed" as success.

    Telegram rejects an edit to identical text and markup - a double tap on
    the same button does exactly that. Returns None in that case.
    """
    try:
        return await message.edit_text(text, **kwargs)
    except TelegramBadRequest as error:
        if _NOT_MODIFIED in error.message:
            return None
        raise
