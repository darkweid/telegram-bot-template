from loggers import get_logger
from src.broadcast.repositories import BroadcastRepository
from src.broadcast.services import stale_before
from src.broadcast.task_names import RUN_BROADCAST
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.utils.datetime_utils import get_utc_now

logger = get_logger(__name__)


class ResumeStalledBroadcastsUseCase:
    """Queue a run for every broadcast whose worker went away.

    That covers a broadcast left mid-run and a finished one whose final report
    never went out; for the latter the run only sends the report. The run
    claims the broadcast through its lease, so a run queued for a broadcast
    that turns out to be alive only exits.
    """

    def __init__(
        self, uow: SQLAlchemyUnitOfWork, broadcasts: BroadcastRepository
    ) -> None:
        self._uow = uow
        self._broadcasts = broadcasts

    async def execute(self) -> int:
        async with self._uow as uow:
            before = stale_before(get_utc_now())
            stalled = await self._broadcasts.find_stalled(uow.session, before)
            unreported = await self._broadcasts.find_unreported(uow.session, before)
            for broadcast_id in (*stalled, *unreported):
                await uow.enqueue_task(RUN_BROADCAST, broadcast_id=str(broadcast_id))
            await uow.commit()
        for broadcast_id in stalled:
            logger.warning("Broadcast %s stalled; queued to resume", broadcast_id)
        for broadcast_id in unreported:
            logger.warning(
                "Broadcast %s finished unreported; queued to report", broadcast_id
            )
        return len(stalled) + len(unreported)
