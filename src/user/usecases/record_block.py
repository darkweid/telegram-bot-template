from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from loggers import get_logger
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.utils.datetime_utils import get_utc_now
from src.user.repositories import UserRepository

logger = get_logger(__name__)


class RecordBlockUseCase:
    """Mark a user the bot can no longer reach (blocked, deactivated) as blocked.

    Called from the Bot API session, not from a handler, so it opens a
    session of its own: the call that failed may have been made outside any
    update, and inside one it must not commit the handler's transaction.
    """

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], users: UserRepository
    ) -> None:
        self._sessionmaker = sessionmaker
        self._users = users

    async def record_blocked(self, user_id: int) -> None:
        async with self._sessionmaker() as session:
            found = await self._mark_blocked(session, user_id)
        logger.info(
            "User %s is unreachable, marked blocked%s",
            user_id,
            "" if found else " (not stored yet)",
        )

    async def _mark_blocked(self, session: AsyncSession, user_id: int) -> bool:
        async with SQLAlchemyUnitOfWork(session) as uow:
            found = await self._users.set_blocked(
                uow.session, user_id, True, get_utc_now()
            )
            await uow.commit()
        return found
