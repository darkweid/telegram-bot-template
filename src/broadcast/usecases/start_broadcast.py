from uuid import UUID

import uuid6

from src.broadcast.exceptions import BroadcastInProgressError
from src.broadcast.repositories import BroadcastRepository
from src.broadcast.task_names import RUN_BROADCAST
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.utils.datetime_utils import get_utc_now
from src.user.repositories import UserRepository


class StartBroadcastUseCase:
    """Store a broadcast and queue its task in one transaction.

    One broadcast at a time: the advisory lock settles two admins confirming
    at once, and the active check refuses while another one is pending or
    running.
    """

    def __init__(
        self,
        uow: SQLAlchemyUnitOfWork,
        broadcasts: BroadcastRepository,
        users: UserRepository,
    ) -> None:
        self._uow = uow
        self._broadcasts = broadcasts
        self._users = users

    async def execute(
        self,
        *,
        admin_id: int,
        source_chat_id: int,
        source_message_id: int,
        progress_message_id: int,
    ) -> UUID:
        async with self._uow as uow:
            if not await self._broadcasts.try_xact_lock(
                uow.session, "start"
            ) or await self._broadcasts.has_active(uow.session):
                raise BroadcastInProgressError
            # The run walks the users present at this moment, the same ones
            # `total` counts; created_at carries the cutoff to it.
            now = get_utc_now()
            total = await self._users.count_recipients(uow.session, registered_by=now)
            # Set here rather than by the column default: the task needs the
            # id before the row is flushed.
            broadcast_id = uuid6.uuid7()
            await self._broadcasts.create(
                uow.session,
                {
                    "id": broadcast_id,
                    "admin_id": admin_id,
                    "source_chat_id": source_chat_id,
                    "source_message_id": source_message_id,
                    "progress_message_id": progress_message_id,
                    "total": total,
                    "created_at": now,
                },
            )
            await uow.enqueue_task(RUN_BROADCAST, broadcast_id=str(broadcast_id))
            await uow.commit()
        return broadcast_id
