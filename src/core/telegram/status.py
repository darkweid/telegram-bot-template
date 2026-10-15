"""The message a user watches while background work runs: sent by the
handler, kept alive by the work, edited into the answer at the end."""

import asyncio
from collections.abc import Awaitable
from contextlib import suppress
from types import TracebackType
from typing import Self

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message
from aiogram.utils.i18n import I18n

from loggers import get_logger
from src.core.i18n import N_
from src.core.telegram.errors import error_reply_text
from src.core.telegram.messages import safe_edit_text
from src.core.telegram.session import without_flood_retry

logger = get_logger(__name__)

WORKING = N_("Working on it…")
INTERRUPTED = N_("Interrupted. Please try again.")
PREFIX = "⏳ "
# Telegram clears a chat action after 5 seconds.
TYPING_INTERVAL_SECONDS: float = 4
EDIT_INTERVAL_SECONDS: float = 1
# A stopping bot waits for this edit; past it the user keeps the last status.
INTERRUPTED_EDIT_SECONDS: float = 2
_GONE = "message to edit not found"


class ProcessingStatus:
    """`send` in the handler, `async with` and `finish` in the work.

    Inside the block a ticker keeps the typing indicator on and shows the
    latest `update`; the work never waits for Telegram. Leaving the block
    with an exception edits the status into what the update's error handler
    would have said, a cancel into "interrupted", and a clean exit without
    `finish` into the generic error, raising RuntimeError: the work forgot
    its answer. Exceptions always propagate. Texts are HTML: the caller
    quotes user values.
    """

    def __init__(self, bot: Bot, message: Message, *, i18n: I18n, locale: str) -> None:
        self._bot = bot
        self._message = message
        self._i18n = i18n
        self._locale = locale
        self._shown = message.text
        self._wanted: str | None = None
        self._changed = asyncio.Event()
        self._ticker: asyncio.Task[None] | None = None
        self._finished = False

    @classmethod
    async def send(
        cls, bot: Bot, chat_id: int, *, i18n: I18n, text: str | None = None
    ) -> Self:
        """Send the status in the current locale, which later texts keep
        whatever context the work runs in. `text` defaults to "Working on
        it…"."""
        shown = text if text is not None else i18n.gettext(WORKING)
        message = await bot.send_message(chat_id, PREFIX + shown)
        return cls(bot, message, i18n=i18n, locale=i18n.current_locale)

    def update(self, text: str) -> None:
        """Ask for `text` to be shown. A text the ticker has not shown yet
        is replaced by the next one."""
        self._wanted = PREFIX + text
        self._changed.set()

    async def finish(
        self, text: str, reply_markup: InlineKeyboardMarkup | None = None
    ) -> None:
        """Edit the status into the answer, with flood control retries; a
        status the user deleted gets the answer as a new message. A second
        finish raises RuntimeError."""
        if self._finished:
            raise RuntimeError("the status is already finished")
        self._finished = True
        await self._stop_ticker()
        await self._show(text, reply_markup)

    async def __aenter__(self) -> Self:
        self._ticker = asyncio.create_task(self._tick())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        await self._stop_ticker()
        if self._finished:
            return False
        self._finished = True
        if isinstance(exc, asyncio.CancelledError):
            # Without flood retries and under a bound: a stopping bot waits.
            with suppress(TelegramAPIError, TimeoutError), without_flood_retry():
                async with asyncio.timeout(INTERRUPTED_EDIT_SECONDS):
                    await self._show(
                        self._i18n.gettext(INTERRUPTED, locale=self._locale)
                    )
            return False
        failure = exc if exc is not None else RuntimeError()
        try:
            await self._show(error_reply_text(self._i18n, self._locale, failure))
        except TelegramAPIError as error:
            # The work's own exception matters more than this one.
            logger.warning(
                "Could not tell the user about the error: %s", type(error).__name__
            )
        if exc is None:
            raise RuntimeError("background work ended without finishing its status")
        return False

    async def _show(
        self, text: str, reply_markup: InlineKeyboardMarkup | None = None
    ) -> None:
        try:
            await safe_edit_text(self._message, text, reply_markup=reply_markup)
        except TelegramBadRequest as error:
            if _GONE not in error.message:
                raise
            await self._bot.send_message(
                self._message.chat.id, text, reply_markup=reply_markup
            )

    async def _stop_ticker(self) -> None:
        ticker, self._ticker = self._ticker, None
        if ticker is None:
            return
        ticker.cancel()
        # wait, not await: a cancel of the work itself must still reach it.
        await asyncio.wait([ticker])
        if not ticker.cancelled() and (error := ticker.exception()) is not None:
            logger.error("Status ticker failed", exc_info=error)

    async def _tick(self) -> None:
        loop = asyncio.get_running_loop()
        next_typing = next_edit = loop.time()
        with without_flood_retry():
            while True:
                now = loop.time()
                if now >= next_typing:
                    next_typing = now + TYPING_INTERVAL_SECONDS
                    wait = await self._attempt(
                        "typing",
                        self._bot.send_chat_action(
                            self._message.chat.id, ChatAction.TYPING
                        ),
                    )
                    if wait is not None:
                        quiet = loop.time() + wait
                        next_typing, next_edit = quiet, max(next_edit, quiet)
                wanted = self._wanted
                if wanted != self._shown and wanted is not None and now >= next_edit:
                    next_edit = now + EDIT_INTERVAL_SECONDS
                    wait = await self._attempt(
                        "edit", safe_edit_text(self._message, wanted)
                    )
                    if wait is None:
                        self._shown = wanted
                    else:
                        quiet = loop.time() + wait
                        next_edit, next_typing = quiet, max(next_typing, quiet)
                self._changed.clear()
                wake = next_typing
                if self._wanted is not None and self._wanted != self._shown:
                    wake = min(wake, next_edit)
                with suppress(TimeoutError):
                    async with asyncio.timeout_at(wake):
                        await self._changed.wait()

    async def _attempt(self, what: str, call: Awaitable[object]) -> float | None:
        """Telegram's wait after flood control, which the ticker keeps quiet
        through before it tries again; None when the call went through or
        failed for another reason, which skips it for good."""
        try:
            await call
        except TelegramRetryAfter as error:
            logger.warning(
                "Status %s skipped: flood control for %ss", what, error.retry_after
            )
            return float(error.retry_after)
        except TelegramAPIError as error:
            logger.warning("Status %s failed: %s", what, type(error).__name__)
        return None
