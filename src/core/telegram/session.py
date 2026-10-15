"""Bot API session middlewares: they see every call the bot makes.

Registered on the bot's session (src/main/di.py). The first registered is the
outermost: aiogram wraps them in reverse order.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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
# minutes or hours when a bot is being punished. An update sleeps while holding
# a polling slot and the user's event-isolation lock (EVENT_ISOLATION_LOCK_SECONDS
# in fsm.py), so all calls of one update share this much sleep, and a wait that
# would go past it fails its call instead. A call outside an update gets the
# same amount to itself.
MAX_RETRY_SLEEP_SECONDS = 30


class _SleepBudget:
    def __init__(self, seconds: float) -> None:
        self.remaining = seconds


_update_sleep_budget: ContextVar[_SleepBudget | None] = ContextVar(
    "update_sleep_budget", default=None
)


@contextmanager
def update_sleep_budget() -> Iterator[None]:
    """Make the flood-wait calls of the block share MAX_RETRY_SLEEP_SECONDS."""
    token = _update_sleep_budget.set(_SleepBudget(MAX_RETRY_SLEEP_SECONDS))
    try:
        yield
    finally:
        _update_sleep_budget.reset(token)


_no_flood_retry: ContextVar[bool] = ContextVar("no_flood_retry", default=False)


@contextmanager
def without_flood_retry() -> Iterator[None]:
    """Make the calls of the block fail on flood control at once instead of
    sleeping it out: for calls the next one makes obsolete (a progress edit,
    a typing action), whose sleep would only hold up the work behind them.

    A task started inside the block inherits the mark; one started before it
    does not see it.
    """
    token = _no_flood_retry.set(True)
    try:
        yield
    finally:
        _no_flood_retry.reset(token)


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
        budget = _update_sleep_budget.get() or _SleepBudget(MAX_RETRY_SLEEP_SECONDS)
        retries = 0
        while True:
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as error:
                if (
                    _no_flood_retry.get()
                    or retries >= self._attempts
                    or error.retry_after > budget.remaining
                ):
                    raise
                retries += 1
                budget.remaining -= error.retry_after
                logger.warning(
                    "Flood control on %s: retry %d/%d in %ss",
                    type(method).__name__,
                    retries,
                    self._attempts,
                    error.retry_after,
                )
                await self._sleep(error.retry_after)


class UnreachableUserRecorder(Protocol):
    async def record_unreachable(self, user_id: int) -> None: ...


class ForbiddenMiddleware(BaseRequestMiddleware):
    """Record a private chat the bot can no longer reach.

    Any Forbidden for a positive integer chat id (a private chat, and so a
    user) marks the user unreachable: a blocked bot, a deactivated account
    and "bot can't initiate conversation" all mean broadcasts would only
    waste calls. A Forbidden from a group or channel means the bot was
    removed from it, which says nothing about a user. The error is re-raised
    either way: the caller still has to know its call failed.
    """

    def __init__(self, recorder: UnreachableUserRecorder) -> None:
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
                    await self._recorder.record_unreachable(chat_id)
                except Exception:
                    # The Telegram error is what the caller must see; a
                    # failure to record it is ours to log, not theirs.
                    logger.exception("Could not mark user %s as unreachable", chat_id)
            raise
