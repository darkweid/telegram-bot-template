from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from src.core.telegram.context import sentry_scope_for, update_context_from
from src.core.update_context import bind_update_context


class UpdateContextMiddleware(BaseMiddleware):
    """Bind the update's ids for logs and Sentry while it is handled.

    No timing here: aiogram's dispatcher already logs every update with its
    duration ("Update id=... is handled. Duration ... ms").
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):  # registered on dp.update only
            return await handler(event, data)
        context = update_context_from(event)
        with bind_update_context(context), sentry_scope_for(context):
            return await handler(event, data)
