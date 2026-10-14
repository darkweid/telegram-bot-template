from uuid import UUID

from src.broadcast.repositories import BroadcastRepository
from src.core.database.uow import SQLAlchemyUnitOfWork


class StopBroadcastUseCase:
    """Ask the running broadcast to stop; it does at its next checkpoint."""

    def __init__(
        self, uow: SQLAlchemyUnitOfWork, broadcasts: BroadcastRepository
    ) -> None:
        self._uow = uow
        self._broadcasts = broadcasts

    async def execute(self, broadcast_id: UUID) -> bool:
        """False when the broadcast has already finished."""
        async with self._uow as uow:
            requested = await self._broadcasts.request_stop(uow.session, broadcast_id)
            await uow.commit()
        return requested
