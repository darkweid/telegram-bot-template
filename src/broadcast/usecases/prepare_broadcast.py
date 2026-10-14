from src.broadcast.exceptions import BroadcastInProgressError
from src.broadcast.repositories import BroadcastRepository
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.user.repositories import UserRepository


class PrepareBroadcastUseCase:
    """Refuse early while a broadcast is active; otherwise count recipients.

    Only a read: StartBroadcastUseCase makes the final check under its lock.
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

    async def execute(self) -> int:
        async with self._uow as uow:
            if await self._broadcasts.has_active(uow.session):
                raise BroadcastInProgressError
            return await self._users.count_recipients(uow.session)
