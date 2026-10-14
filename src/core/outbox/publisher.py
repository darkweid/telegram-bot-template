"""Sending stored outbox rows to the task broker."""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import AsyncBroker

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.outbox.repositories import OutboxRepository


class UnregisteredTaskError(LookupError):
    """No task of this name is registered with the broker: no worker would
    ever run it."""


async def kick_stored(
    broker: AsyncBroker,
    message_id: UUID,
    task_name: str,
    args: list[Any],
    kwargs: dict[str, Any],
) -> None:
    """Send one outbox row as a task.

    Through the registered task's kicker, so its labels (retry_on_error)
    travel with it; under the row's id, so a row sent twice runs once.
    """
    task = broker.find_task(task_name)
    if task is None:
        raise UnregisteredTaskError(task_name)
    await task.kicker().with_task_id(str(message_id)).kiq(*args, **kwargs)


class OutboxPublisher:
    """Sends an outbox row right after its transaction commits.

    Best-effort: when the send or the mark fails, the after-commit hook logs
    it and the sweeper sends the row within a couple of minutes.
    """

    def __init__(
        self, broker: AsyncBroker, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        self._broker = broker
        self._sessionmaker = sessionmaker
        self._outbox = OutboxRepository()

    async def publish(
        self, message_id: UUID, task_name: str, args: list[Any], kwargs: dict[str, Any]
    ) -> None:
        await kick_stored(self._broker, message_id, task_name, args, kwargs)
        # A session of its own: the update's session has just committed and
        # belongs to the use case that is still returning.
        async with self._sessionmaker() as session:
            async with SQLAlchemyUnitOfWork(session) as uow:
                await self._outbox.mark_published(uow.session, message_id)
                await uow.commit()
