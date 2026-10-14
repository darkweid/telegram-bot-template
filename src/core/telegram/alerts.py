"""Telling developers that something broke, in Telegram."""

import asyncio
from dataclasses import dataclass
import hashlib
import traceback
from typing import NewType, Protocol

from aiogram import Bot, html
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramAPIError

from loggers import get_logger
from src.core.limiter.limiter import RateLimiter

logger = get_logger(__name__)

# The alert is one Telegram message; an error message can be anything long.
MAX_ERROR_MESSAGE_LENGTH = 500

# How long a task id that failed for good is remembered. A delivery withheld
# while another worker held the task is reclaimed after the stream's idle
# timeout and can run, and fail, once more: by then the cooldown may be over,
# so the id is kept far longer than any redelivery can take.
TASK_ALERT_MEMORY_MS = 24 * 60 * 60 * 1000

# Lets tests replace the cooldown limiter alone, on a fake Redis.
AlertLimiter = NewType("AlertLimiter", RateLimiter)
# Alerts leave through a Bot of their own, apart from the one handlers use. The
# handlers' Bot reports a Forbidden to the user domain, whose recorder needs
# the cache, and the cache reports its Redis outages through the alerter: one
# Bot for both would be a dependency cycle. The alert chat is no user anyway.
AlertSession = NewType("AlertSession", BaseSession)
AlertBot = NewType("AlertBot", Bot)


@dataclass(frozen=True, slots=True)
class Alert:
    """One failure, as developers see it. `error` has already been through
    `reportable`: no driver DETAIL, no rejected input."""

    error: BaseException
    # "update message", "task outbox.sweep": what was being handled.
    origin: str
    user_id: int | None = None
    sentry_event_id: str | None = None
    # A failed task's id: one task is alerted once, however often it is redelivered.
    task_id: str | None = None


class TaskFailureReporter(Protocol):
    async def alert_error(self, alert: Alert) -> None: ...


def _raised_in(error: BaseException) -> str:
    """The function the error was raised in, `file:function`; line numbers
    are left out so a fingerprint survives an unrelated edit above."""
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return "-"
    frame = frames[-1]
    return f"{frame.filename.rsplit('/', 1)[-1]}:{frame.name}"


class DeveloperAlerter:
    """Sends failures to the alert chat, or to every developer in private.

    Shared by the bot and the worker. The same failure (type, origin, place)
    is sent once per cooldown. Sending is direct, not through the outbox: an
    alert must leave even when the database is what broke. A delivery that
    fails is logged and nothing more.
    """

    def __init__(
        self,
        bot: Bot,
        limiter: RateLimiter,
        *,
        recipients: tuple[int, ...],
        cooldown_seconds: int,
    ) -> None:
        self._bot = bot
        self._limiter = limiter
        self._recipients = recipients
        self._cooldown_ms = cooldown_seconds * 1000
        self._background: set[asyncio.Task[None]] = set()

    async def alert_error(self, alert: Alert) -> None:
        # The task id is counted first, so a task whose first failure fell
        # inside its fingerprint's cooldown stays silent on a redelivery too.
        if alert.task_id is not None and await self._limiter.hit(
            f"task:{alert.task_id}", limit=1, window_ms=TASK_ALERT_MEMORY_MS
        ):
            return
        raised_in = _raised_in(alert.error)
        fingerprint = hashlib.sha256(
            f"{type(alert.error).__qualname__}|{alert.origin}|{raised_in}".encode()
        ).hexdigest()[:32]
        if await self._limiter.hit(fingerprint, limit=1, window_ms=self._cooldown_ms):
            return
        await self.send(_format(alert, raised_in))

    async def send(self, text: str) -> None:
        for chat_id in self._recipients:
            try:
                await self._bot.send_message(chat_id, text)
            except TelegramAPIError as error:
                logger.warning(
                    "Alert not delivered to %s: %s", chat_id, type(error).__name__
                )

    def notify(self, message: str) -> None:
        """DegradationNotifier: called from synchronous code, so the message
        leaves in a background task."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Alert not sent: no running event loop")
            return
        task = loop.create_task(self.send(html.quote(message)))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain(self) -> None:
        """Wait for background alerts; the container calls it before the bot
        session closes."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)


def _format(alert: Alert, raised_in: str) -> str:
    message = str(alert.error)
    if len(message) > MAX_ERROR_MESSAGE_LENGTH:
        message = message[:MAX_ERROR_MESSAGE_LENGTH] + "..."
    lines = [
        f"<b>{html.quote(type(alert.error).__name__)}</b>"
        f" in {html.quote(alert.origin)}",
        f"at {html.quote(raised_in)}",
    ]
    if alert.user_id is not None:
        lines.append(f"user {alert.user_id}")
    if alert.task_id is not None:
        lines.append(f"task id {html.quote(alert.task_id)}")
    if alert.sentry_event_id is not None:
        lines.append(f"sentry {html.quote(alert.sentry_event_id)}")
    lines.append(f"<pre>{html.quote(message)}</pre>")
    return "\n".join(lines)
