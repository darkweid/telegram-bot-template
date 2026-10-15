"""Background upkeep of the LLM layer."""

from datetime import timedelta
from typing import NewType

from dishka import FromDishka
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.llm.repository import LLMUsageRepository
from src.core.utils.datetime_utils import get_utc_now

# How long llm_usage rows are kept. Provided by the composition root from
# LLM_USAGE_RETENTION_DAYS: core does not read the config.
LLMUsageRetention = NewType("LLMUsageRetention", timedelta)
# Rows per DELETE: each statement stays far inside the connection's statement
# timeout, even when a shorter retention leaves months of rows to drop.
LLM_USAGE_PURGE_BATCH_SIZE = 1000


async def purge_llm_usage(
    session: FromDishka[AsyncSession],
    usage: FromDishka[LLMUsageRepository],
    retention: FromDishka[LLMUsageRetention],
) -> str:
    """Delete usage rows older than the retention window, in batches of
    LLM_USAGE_PURGE_BATCH_SIZE.

    Each batch commits in a unit of work of its own: in one transaction a
    failure on a late batch would roll back every earlier one, and a large
    backlog would hold its locks for the whole run.
    """
    cutoff = get_utc_now() - retention
    deleted = 0
    while True:
        async with SQLAlchemyUnitOfWork(session) as uow:
            batch = await usage.purge_older_than(
                uow.session, cutoff, limit=LLM_USAGE_PURGE_BATCH_SIZE
            )
            await uow.commit()
        deleted += batch
        if batch < LLM_USAGE_PURGE_BATCH_SIZE:
            break
    return f"Deleted {deleted} LLM usage rows."
