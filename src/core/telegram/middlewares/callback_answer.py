from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, TelegramObject
from aiogram.utils.callback_answer import CallbackAnswer, CallbackAnswerMiddleware


class SkipAwareCallbackAnswerMiddleware(CallbackAnswerMiddleware):
    """Leave the answer to the next handler when one passes a callback on.

    A handler that raises SkipHandler before its answer was sent hands the
    callback to the next matching handler, which this middleware wraps again
    and answers; answering for the skipping handler as well would answer the
    query twice. A handler flagged to answer first (`pre=True`) has already
    answered when it skips, so a skip there still ends in two answers.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        async def handler_or_skip(event: TelegramObject, data: dict[str, Any]) -> Any:
            try:
                return await handler(event, data)
            except SkipHandler:
                callback_answer: CallbackAnswer = data["callback_answer"]
                if not callback_answer.answered:
                    callback_answer.disabled = True
                raise

        return await super().__call__(handler_or_skip, event, data)
