from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Any, NewType

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import CallbackQuery, Message, TelegramObject, User
from aiogram.utils.callback_answer import CallbackAnswer
from aiogram.utils.i18n import ngettext

from loggers import get_logger
from src.core.limiter.limiter import RateLimiter
from src.core.telegram.roles import Roles

logger = get_logger(__name__)


class Throttle(StrEnum):
    DEFAULT = "default"
    # For handlers that call a paid model: a flood there costs money.
    LLM = "llm"


@dataclass(frozen=True, slots=True)
class ThrottleRule:
    limit: int
    window_seconds: int


# Lets tests replace the limiter alone, on a fake Redis.
ThrottleLimiter = NewType("ThrottleLimiter", RateLimiter)


class ThrottlingMiddleware(BaseMiddleware):
    """Limit how often one user reaches a handler.

    An inner middleware: the `throttle` flag is known only once a handler is
    chosen, and an update no handler takes costs nothing worth limiting.
    Admins and developers are never limited. Over the limit, the first
    refused update in the window is told to wait and the rest are dropped.
    """

    def __init__(
        self, limiter: RateLimiter, rules: Mapping[Throttle, ThrottleRule]
    ) -> None:
        missing = set(Throttle) - set(rules)
        if missing:
            raise ValueError(f"No throttle rule for {sorted(missing)}")
        self._limiter = limiter
        self._rules = dict(rules)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        sender: User | None = data.get("event_from_user")
        roles: Roles = data["roles"]
        if sender is None or roles.is_privileged(sender.id):
            return await handler(event, data)
        profile = Throttle(get_flag(data, "throttle", default=Throttle.DEFAULT))
        rule = self._rules[profile]
        key = f"{profile}:{sender.id}"
        retry_ms = await self._limiter.hit(
            key, limit=rule.limit, window_ms=rule.window_seconds * 1000
        )
        if retry_ms == 0:
            return await handler(event, data)
        # A second counter with room for one: the first refusal of the window
        # is answered, so a flood cannot make the bot flood back.
        if await self._limiter.hit(f"{key}:notice", limit=1, window_ms=retry_ms) == 0:
            logger.info("User %s throttled (%s)", sender.id, profile.value)
            await self._warn(event, data, math.ceil(retry_ms / 1000))
        return None

    async def _warn(
        self, event: TelegramObject, data: dict[str, Any], seconds: int
    ) -> None:
        text = ngettext(
            "Too many requests. Try again in {seconds} second.",
            "Too many requests. Try again in {seconds} seconds.",
            seconds,
        ).format(seconds=seconds)
        if isinstance(event, CallbackQuery):
            # SkipAwareCallbackAnswerMiddleware answers every query; a second
            # answer from here would be refused.
            answer: CallbackAnswer = data["callback_answer"]
            answer.text = text
            return
        if isinstance(event, Message):
            await event.answer(text)
