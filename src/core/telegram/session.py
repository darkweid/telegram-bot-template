"""Bot API session middlewares: they see every call the bot makes.

Registered on the bot's session (src/main/di.py). The first registered is the
outermost: aiogram wraps them in reverse order.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType

from loggers import get_logger

logger = get_logger(__name__)

# Telegram asks for waits of a few seconds under normal flood control and for
# minutes or hours when a bot is being punished. Sleeping that long inside an
# update would hold the user's event-isolation lock and a polling slot, so a
# longer wait fails the call instead.
MAX_RETRY_AFTER_SECONDS = 60


class RetryAfterMiddleware(BaseRequestMiddleware):
    def __init__(
        self,
        attempts: int,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._attempts = attempts
        self._sleep = sleep

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        retries = 0
        while True:
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as error:
                if (
                    retries >= self._attempts
                    or error.retry_after > MAX_RETRY_AFTER_SECONDS
                ):
                    raise
                retries += 1
                logger.warning(
                    "Flood control on %s: retry %d/%d in %ss",
                    type(method).__name__,
                    retries,
                    self._attempts,
                    error.retry_after,
                )
                await self._sleep(error.retry_after)


class BlockedUserRecorder(Protocol):
    async def record_blocked(self, user_id: int) -> None: ...


class ForbiddenMiddleware(BaseRequestMiddleware):
    """Record a private chat the bot can no longer reach.

    Any Forbidden for a positive integer chat id (a private chat, and so a
    user) marks the user: a blocked bot, a deactivated account and "bot can't
    initiate conversation" all mean broadcasts would only waste calls. A
    Forbidden from a group or channel means the bot was removed from it,
    which is not a user's block. The error is re-raised either way: the
    caller still has to know its call failed.
    """

    def __init__(self, recorder: BlockedUserRecorder) -> None:
        self._recorder = recorder

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        try:
            return await make_request(bot, method)
        except TelegramForbiddenError:
            chat_id: Any = getattr(method, "chat_id", None)
            if isinstance(chat_id, int) and chat_id > 0:
                try:
                    await self._recorder.record_blocked(chat_id)
                except Exception:
                    # The Telegram error is what the caller must see; a
                    # failure to record it is ours to log, not theirs.
                    logger.exception(
                        "Could not mark unreachable user %s as blocked", chat_id
                    )
            raise
