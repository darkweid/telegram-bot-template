"""Sending stored outbox rows to the task broker."""

import asyncio
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import AsyncBroker
from taskiq.exceptions import SendTaskError

from loggers import get_logger
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.outbox.repositories import OutboxRepository
from src.core.redis.errors import FAIL_OPEN_ERRORS

logger = get_logger(__name__)

# The longest a broker start or one send may take. Both run inside an update's
# commit (the after-commit hook) or under the sweeper's row locks, so a
# blackholed Redis must cost seconds, not the kernel's TCP timeout. Matches
# the socket timeouts of the project's Redis clients.
SEND_TIMEOUT_SECONDS = 5.0

# A broker that cannot be reached, or a full Redis refusing writes, says
# nothing about the message: counting it as an attempt would fail every
# pending row during an outage.
BROKER_UNREACHABLE_ERRORS: tuple[type[BaseException], ...] = (
    *FAIL_OPEN_ERRORS,
    ConnectionError,
    TimeoutError,
)


class UnregisteredTaskError(LookupError):
    """No task of this name is registered with the broker: no worker would
    ever run it."""


def broker_error(error: BaseException) -> BaseException:
    """What the broker itself raised: taskiq's kicker wraps every error of
    `broker.kick` in SendTaskError, which tells an outage from a refusal
    nothing."""
    if isinstance(error, SendTaskError) and error.__cause__ is not None:
        return error.__cause__
    return error


def is_broker_unreachable(error: BaseException) -> bool:
    """True when a send failed for the broker's sake, not the message's."""
    return isinstance(broker_error(error), BROKER_UNREACHABLE_ERRORS)


async def kick_stored(
    broker: AsyncBroker,
    message_id: UUID,
    task_name: str,
    args: list[Any],
    kwargs: dict[str, Any],
) -> None:
    """Send one outbox row as a task.

    Through the registered task's kicker, so its labels (retry_on_error)
    travel with it; under the row's id, so a row sent twice runs once. A send
    still running after SEND_TIMEOUT_SECONDS raises TimeoutError.
    """
    task = broker.find_task(task_name)
    if task is None:
        raise UnregisteredTaskError(task_name)
    async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
        await task.kicker().with_task_id(str(message_id)).kiq(*args, **kwargs)


class OutboxPublisher:
    """Sends an outbox row right after its transaction commits.

    Best-effort: when the send or the mark fails, the row stays pending and
    the sweeper sends it within a couple of minutes.

    With `start_broker` the publisher starts the broker on its first send.
    The bot has no taskiq CLI to start it, and starting it while the
    container resolves a unit of work would fail every update during a Redis
    outage, the ones that enqueue nothing included. The worker's broker is
    started by the CLI and must not be started twice.
    """

    def __init__(
        self,
        broker: AsyncBroker,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        start_broker: bool = False,
    ) -> None:
        self._broker = broker
        self._sessionmaker = sessionmaker
        self._outbox = OutboxRepository()
        self._needs_start = start_broker
        self._start_lock = asyncio.Lock()
        # Failed starts so far: a send that queued behind a start tells by
        # it whether that start failed.
        self._failed_starts = 0

    async def publish(
        self, message_id: UUID, task_name: str, args: list[Any], kwargs: dict[str, Any]
    ) -> None:
        try:
            await self._ensure_started()
            await kick_stored(self._broker, message_id, task_name, args, kwargs)
        except Exception as error:
            if not is_broker_unreachable(error):
                raise
            # An outage is the sweeper's to handle: one line, no traceback
            # per update.
            logger.warning(
                "Task %s not sent, the broker is unreachable (%s); "
                "the outbox sweeper sends it",
                task_name,
                type(broker_error(error)).__name__,
            )
            return
        # A session of its own: the update's session has just committed and
        # belongs to the use case that is still returning.
        async with self._sessionmaker() as session:
            async with SQLAlchemyUnitOfWork(session) as uow:
                await self._outbox.mark_published(uow.session, message_id)
                await uow.commit()

    async def _ensure_started(self) -> None:
        if not self._needs_start:
            return
        failed_before = self._failed_starts
        # The wait for the lock counts against the same bound as the start:
        # concurrent first sends behind a hanging start must not each wait
        # their own SEND_TIMEOUT_SECONDS in turn, past the update's lock.
        async with asyncio.timeout(SEND_TIMEOUT_SECONDS), self._start_lock:
            if not self._needs_start:
                return
            if self._failed_starts != failed_before:
                # A start failed while this send waited: trying again at once
                # would only queue the next sends behind another one.
                raise ConnectionError("The broker failed to start a moment ago")
            try:
                await self._broker.startup()
            except BaseException:
                # BaseException: a start cut off by the timeout failed too.
                self._failed_starts += 1
                raise
            self._needs_start = False
