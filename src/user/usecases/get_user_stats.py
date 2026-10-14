from src.core.database.transactions import set_local_statement_timeout
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.utils.datetime_utils import get_utc_now
from src.user.repositories import UserRepository
from src.user.schemas import UserStats

# The stats query walks the whole users table. Below the session-wide
# DB_STATEMENT_TIMEOUT_SECONDS: an admin command must not hold a pool
# connection as long as an update may.
STATS_TIMEOUT_SECONDS = 10


class GetUserStatsUseCase:
    """User counts for an admin's /stats. Read-only: the unit of work is
    left without a commit and rolls back."""

    def __init__(self, uow: SQLAlchemyUnitOfWork, users: UserRepository) -> None:
        self._uow = uow
        self._users = users

    async def execute(self) -> UserStats:
        async with self._uow as uow:
            await set_local_statement_timeout(uow.session, STATS_TIMEOUT_SECONDS)
            return await self._users.get_stats(uow.session, get_utc_now())
