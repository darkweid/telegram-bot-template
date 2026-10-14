import asyncio
from dataclasses import dataclass
from datetime import datetime
import time
from uuid import UUID, uuid4

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.i18n import I18n
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from loggers import get_logger
from src.broadcast.enums import BroadcastStatus
from src.broadcast.keyboards import stop_keyboard
from src.broadcast.repositories import BroadcastRepository
from src.broadcast.schemas import BroadcastProgress, Checkpoint
from src.broadcast.services import stale_before
from src.broadcast.texts import progress_text
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.i18n import resolve_locale
from src.core.limiter.pacer import Pacer
from src.core.utils.datetime_utils import get_utc_now
from src.main.config import BotConfig, BroadcastConfig
from src.user.repositories import UserRepository

logger = get_logger(__name__)

# The admin's progress message is edited at most this often.
PROGRESS_EDIT_INTERVAL_SECONDS = 5
# The longest a run goes without a checkpoint, flood waits included; well
# under STALE_AFTER, after which the resume task hands the broadcast over.
CHECKPOINT_INTERVAL_SECONDS = 60
# Pauses before retrying a copy that met a network or server error; one more
# failure after the last of them fails the broadcast.
TRANSIENT_RETRY_DELAYS_SECONDS: tuple[float, ...] = (1, 2, 4, 8)
# Telegram's answer when the broadcast's source message is gone: no copy can
# succeed, so it fails the broadcast rather than one recipient.
SOURCE_MISSING_ERROR = "message to copy not found"


class _LeaseLostError(Exception):
    """Another run took the broadcast over; this one must stop touching it."""


@dataclass(slots=True)
class _Run:
    id: UUID
    run_id: UUID
    admin_id: int
    source_chat_id: int
    source_message_id: int
    progress_message_id: int
    registered_by: datetime
    total: int
    sent: int
    failed: int
    unreachable: int
    cursor: int
    stop_requested: bool
    locale: str
    checkpointed_at: float
    edited_at: float | None = None

    def checkpoint(self) -> Checkpoint:
        return Checkpoint(
            cursor_user_id=self.cursor,
            sent=self.sent,
            failed=self.failed,
            unreachable=self.unreachable,
        )

    def progress(self, status: BroadcastStatus) -> BroadcastProgress:
        return BroadcastProgress(
            self.id,
            status,
            total=self.total,
            sent=self.sent,
            failed=self.failed,
            unreachable=self.unreachable,
        )


class RunBroadcastUseCase:
    """Copy the broadcast's message to every recipient, from its checkpoint on.

    Runs in the worker. Every read and write opens its own short session, so
    no connection is held while messages go out. A run owns the broadcast
    through its lease (`run_id`), renewed at each checkpoint; a run that finds
    its lease taken quits without writing anything.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        broadcasts: BroadcastRepository,
        users: UserRepository,
        bot: Bot,
        i18n: I18n,
        pacer: Pacer,
        broadcast_config: BroadcastConfig,
        bot_config: BotConfig,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._broadcasts = broadcasts
        self._users = users
        self._bot = bot
        self._i18n = i18n
        self._pacer = pacer
        self._batch_size = broadcast_config.BROADCAST_CHECKPOINT_EVERY
        self._default_locale = bot_config.DEFAULT_LOCALE

    async def execute(self, broadcast_id: UUID) -> BroadcastStatus | None:
        """The final status, or None when another run owns the broadcast or it
        has already finished."""
        run = await self._claim(broadcast_id)
        if run is None:
            logger.info("Broadcast %s is finished or owned by a live run", broadcast_id)
            return None
        try:
            if run.stop_requested:
                status = BroadcastStatus.CANCELLED
            else:
                # Shown before the first copy: the Stop button is there at once.
                await self._show_progress(run)
                status = await self._send_all(run)
        except _LeaseLostError:
            logger.warning("Broadcast %s was taken over by another run", run.id)
            return None
        except Exception:
            await self._fail(run)
            raise
        if await self._finish(run, status):
            await self._report(run, status)
        return status

    async def _claim(self, broadcast_id: UUID) -> _Run | None:
        run_id = uuid4()
        now = get_utc_now()
        async with (
            self._sessionmaker() as session,
            SQLAlchemyUnitOfWork(session) as uow,
        ):
            broadcast = await self._broadcasts.claim_run(
                uow.session,
                broadcast_id,
                run_id,
                now=now,
                stale_before=stale_before(now),
            )
            if broadcast is None:
                return None
            admin = await self._users.get_single(uow.session, id=broadcast.admin_id)
            candidates = (admin.locale, admin.language_code) if admin else ()
            run = _Run(
                id=broadcast.id,
                run_id=run_id,
                admin_id=broadcast.admin_id,
                source_chat_id=broadcast.source_chat_id,
                source_message_id=broadcast.source_message_id,
                progress_message_id=broadcast.progress_message_id,
                registered_by=broadcast.created_at,
                total=broadcast.total,
                sent=broadcast.sent,
                failed=broadcast.failed,
                unreachable=broadcast.unreachable,
                cursor=broadcast.cursor_user_id,
                stop_requested=broadcast.stop_requested,
                locale=resolve_locale(*candidates, default=self._default_locale),
                checkpointed_at=time.monotonic(),
            )
            await uow.commit()
        return run

    async def _send_all(self, run: _Run) -> BroadcastStatus:
        while recipients := await self._next_batch(run):
            for user_id in recipients:
                if not await self._deliver(run, user_id):
                    return BroadcastStatus.CANCELLED
                run.cursor = user_id
                await self._show_progress(run)
                if (
                    time.monotonic() - run.checkpointed_at
                    >= CHECKPOINT_INTERVAL_SECONDS
                ):
                    await self._checkpoint(run)
                    if run.stop_requested:
                        return BroadcastStatus.CANCELLED
            await self._checkpoint(run)
            if run.stop_requested:
                return BroadcastStatus.CANCELLED
        return BroadcastStatus.COMPLETED

    async def _next_batch(self, run: _Run) -> list[int]:
        async with (
            self._sessionmaker() as session,
            SQLAlchemyUnitOfWork(session) as uow,
        ):
            return await self._users.get_recipient_ids(
                uow.session,
                after=run.cursor,
                limit=self._batch_size,
                registered_by=run.registered_by,
            )

    async def _deliver(self, run: _Run, user_id: int) -> bool:
        """Copy the message to one recipient and count the outcome. False when
        a stop came in while waiting out flood control: the recipient is not
        done, and the cursor stays before them."""
        delays = iter(TRANSIENT_RETRY_DELAYS_SECONDS)
        while True:
            await self._pacer.wait()
            try:
                await self._bot.copy_message(
                    user_id, run.source_chat_id, run.source_message_id
                )
            except TelegramRetryAfter as error:
                if not await self._sit_out(run, error.retry_after):
                    return False
                continue
            except (TelegramNetworkError, TelegramServerError):
                delay = next(delays, None)
                if delay is None:
                    raise
                await asyncio.sleep(delay)
                continue
            except TelegramForbiddenError:
                # ForbiddenMiddleware has already marked the user unreachable.
                run.unreachable += 1
            except (TelegramBadRequest, TelegramNotFound) as error:
                if SOURCE_MISSING_ERROR in error.message:
                    raise
                logger.info(
                    "Broadcast %s skipped user %s: %s",
                    run.id,
                    user_id,
                    type(error).__name__,
                )
                run.failed += 1
            else:
                run.sent += 1
            return True

    async def _sit_out(self, run: _Run, seconds: int) -> bool:
        """Wait out flood control longer than the session middleware's budget,
        renewing the lease every CHECKPOINT_INTERVAL_SECONDS meanwhile. False
        as soon as a checkpoint brings a stop request."""
        logger.warning("Broadcast %s waits %s s of flood control", run.id, seconds)
        remaining = float(seconds)
        while remaining > 0:
            await self._checkpoint(run)
            if run.stop_requested:
                return False
            pause = min(remaining, CHECKPOINT_INTERVAL_SECONDS)
            await asyncio.sleep(pause)
            remaining -= pause
        return True

    async def _checkpoint(self, run: _Run) -> None:
        async with (
            self._sessionmaker() as session,
            SQLAlchemyUnitOfWork(session) as uow,
        ):
            stop = await self._broadcasts.save_checkpoint(
                uow.session,
                run.id,
                run.run_id,
                checkpoint=run.checkpoint(),
                now=get_utc_now(),
            )
            await uow.commit()
        if stop is None:
            raise _LeaseLostError
        run.stop_requested = stop
        run.checkpointed_at = time.monotonic()

    async def _finish(self, run: _Run, status: BroadcastStatus) -> bool:
        async with (
            self._sessionmaker() as session,
            SQLAlchemyUnitOfWork(session) as uow,
        ):
            finished = await self._broadcasts.finish(
                uow.session,
                run.id,
                run.run_id,
                checkpoint=run.checkpoint(),
                status=status,
                now=get_utc_now(),
            )
            await uow.commit()
        if not finished:
            logger.warning("Broadcast %s was taken over before it finished", run.id)
        return finished

    async def _fail(self, run: _Run) -> None:
        """Mark the broadcast failed and tell its admin. The error itself
        leaves the task, and the task middleware alerts developers."""
        try:
            if await self._finish(run, BroadcastStatus.FAILED):
                await self._report(run, BroadcastStatus.FAILED)
        except Exception as error:
            # The error being raised is what matters; this one gets a line.
            logger.warning(
                "Broadcast %s could not be marked failed: %s",
                run.id,
                type(error).__name__,
            )

    async def _show_progress(self, run: _Run) -> None:
        now = time.monotonic()
        if (
            run.edited_at is not None
            and now - run.edited_at < PROGRESS_EDIT_INTERVAL_SECONDS
        ):
            return
        run.edited_at = now
        # A task runs outside any update: context() makes this I18n current.
        with self._i18n.context(), self._i18n.use_locale(run.locale):
            text = progress_text(run.progress(BroadcastStatus.RUNNING))
            markup = stop_keyboard(run.id)
        await self._edit(run, text, markup)

    async def _report(self, run: _Run, status: BroadcastStatus) -> None:
        with self._i18n.context(), self._i18n.use_locale(run.locale):
            text = progress_text(run.progress(status))
        # The edit drops the Stop button; the new message notifies the admin,
        # which an edit does not.
        await self._edit(run, text, None)
        try:
            await self._bot.send_message(run.admin_id, text)
        except TelegramAPIError as error:
            logger.warning(
                "Broadcast %s report was not delivered: %s",
                run.id,
                type(error).__name__,
            )

    async def _edit(
        self, run: _Run, text: str, reply_markup: InlineKeyboardMarkup | None
    ) -> None:
        try:
            # The progress message is in the admin's private chat, whose id is
            # the admin's user id.
            await self._bot.edit_message_text(
                text,
                chat_id=run.admin_id,
                message_id=run.progress_message_id,
                reply_markup=reply_markup,
            )
        except TelegramAPIError as error:
            # The admin may have deleted the message; the broadcast goes on.
            logger.warning(
                "Broadcast %s progress edit failed: %s", run.id, type(error).__name__
            )
