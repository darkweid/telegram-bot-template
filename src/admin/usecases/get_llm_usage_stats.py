from datetime import timedelta

from src.admin.schemas import LLMUsageStats
from src.core.database.transactions import set_local_statement_timeout
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.llm.repository import LLMUsageRepository
from src.core.utils.datetime_utils import get_utc_now

# Below the session-wide DB_STATEMENT_TIMEOUT_SECONDS: an admin command must
# not hold a pool connection as long as an update may.
LLM_STATS_TIMEOUT_SECONDS = 10
TOP_USERS_LIMIT = 5


class GetLLMUsageStatsUseCase:
    """LLM spending for an admin's /stats. Read-only: the unit of work is
    left without a commit and rolls back."""

    def __init__(self, uow: SQLAlchemyUnitOfWork, usage: LLMUsageRepository) -> None:
        self._uow = uow
        self._usage = usage

    async def execute(self) -> LLMUsageStats:
        now = get_utc_now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month = now - timedelta(days=30)
        async with self._uow as uow:
            session = uow.session
            await set_local_statement_timeout(session, LLM_STATS_TIMEOUT_SECONDS)
            return LLMUsageStats(
                today=await self._usage.summary(session, today, now),
                week=await self._usage.summary(session, now - timedelta(days=7), now),
                month=await self._usage.summary(session, month, now),
                by_purpose=await self._usage.by_purpose(session, month, now),
                top_users=await self._usage.top_users(
                    session, month, now, limit=TOP_USERS_LIMIT
                ),
            )
