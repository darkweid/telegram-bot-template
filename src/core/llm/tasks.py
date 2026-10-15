"""Background upkeep of the LLM layer."""

from datetime import timedelta
from functools import partial
from typing import NewType

from dishka import FromDishka
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database.batches import delete_in_batches
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
    LLM_USAGE_PURGE_BATCH_SIZE (see `delete_in_batches`)."""
    purge_batch = partial(usage.purge_older_than, cutoff=get_utc_now() - retention)
    deleted = await delete_in_batches(session, purge_batch, LLM_USAGE_PURGE_BATCH_SIZE)
    return f"Deleted {deleted} LLM usage rows."
