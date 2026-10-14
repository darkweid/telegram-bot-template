from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from loggers import get_logger
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.utils.datetime_utils import get_utc_now
from src.user.repositories import UserRepository

logger = get_logger(__name__)


class RecordUnreachableUseCase:
    """Telegram refuses delivery when the user blocked the bot or deleted their
    account, among other causes.

    Called from the Bot API session, not from a handler, so it opens a
    session of its own: the call that failed may have been made outside any
    update, and inside one it must not commit the handler's transaction.
    """

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], users: UserRepository
    ) -> None:
        self._sessionmaker = sessionmaker
        self._users = users

    async def record_unreachable(self, user_id: int) -> None:
        async with self._sessionmaker() as session:
            found = await self._mark_unreachable(session, user_id)
        logger.info(
            "User %s marked unreachable%s",
            user_id,
            "" if found else " (not stored yet)",
        )

    async def _mark_unreachable(self, session: AsyncSession, user_id: int) -> bool:
        async with SQLAlchemyUnitOfWork(session) as uow:
            found = await self._users.set_reachable(
                uow.session, user_id, False, get_utc_now()
            )
            await uow.commit()
        return found
