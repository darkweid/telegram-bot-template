from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.core.llm import tasks
from src.core.llm.repository import LLMUsageRepository
from src.core.llm.tasks import (
    LLM_USAGE_PURGE_BATCH_SIZE,
    LLMUsageRetention,
    purge_llm_usage,
)
from tests.fakes.db import FakeAsyncSession

NOW = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)
RETENTION = LLMUsageRetention(timedelta(days=90))


async def test_rows_older_than_the_retention_are_deleted_batch_by_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the purge llm_usage grows by a row per model call forever;
    a full batch means more rows may be left, a short one that none are."""
    monkeypatch.setattr(tasks, "get_utc_now", lambda: NOW)
    session = FakeAsyncSession()
    usage = AsyncMock(spec=LLMUsageRepository)
    usage.purge_older_than.side_effect = [LLM_USAGE_PURGE_BATCH_SIZE, 7]

    result = await purge_llm_usage(session, usage, RETENTION)

    cutoff = NOW - timedelta(days=90)
    assert [
        (call.args, call.kwargs) for call in usage.purge_older_than.await_args_list
    ] == [((session,), {"cutoff": cutoff, "limit": LLM_USAGE_PURGE_BATCH_SIZE})] * 2
    assert session.commit.await_count == 2
    assert result == f"Deleted {LLM_USAGE_PURGE_BATCH_SIZE + 7} LLM usage rows."


async def test_a_failed_llm_usage_purge_batch_keeps_the_earlier_ones_deleted() -> None:
    """In one transaction a late failure would undo the whole night's purge,
    and the next run would start again from the same backlog."""
    session = FakeAsyncSession()
    usage = AsyncMock(spec=LLMUsageRepository)
    usage.purge_older_than.side_effect = [
        LLM_USAGE_PURGE_BATCH_SIZE,
        LLM_USAGE_PURGE_BATCH_SIZE,
        ConnectionError("database gone"),
    ]

    with pytest.raises(ConnectionError):
        await purge_llm_usage(session, usage, RETENTION)

    assert session.commit.await_count == 2
    session.rollback.assert_awaited()
