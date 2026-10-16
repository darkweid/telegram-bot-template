from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.admin.schemas import LLMUsageStats
from src.admin.usecases import get_llm_usage_stats
from src.admin.usecases.get_llm_usage_stats import (
    TOP_USERS_LIMIT,
    GetLLMUsageStatsUseCase,
)
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.llm.usage.repository import (
    LLMUsageByPurpose,
    LLMUsageRepository,
    LLMUsageSummary,
    LLMUsageTopUser,
)
from tests.fakes.db import FakeAsyncSession

NOW = datetime(2026, 1, 8, 15, 30, tzinfo=UTC)
EMPTY = LLMUsageSummary(
    calls=0,
    errors=0,
    input_tokens=0,
    output_tokens=0,
    cache_read_tokens=0,
    cache_write_tokens=0,
    cost_usd=Decimal(0),
    unpriced_calls=0,
)


def _usage() -> AsyncMock:
    usage = AsyncMock(spec=LLMUsageRepository)
    usage.summary.return_value = EMPTY
    usage.by_purpose.return_value = [
        LLMUsageByPurpose(purpose="note_summary", calls=1, cost_usd=Decimal("0.01"))
    ]
    usage.top_users.return_value = [
        LLMUsageTopUser(user_id=5, calls=1, cost_usd=Decimal("0.01"))
    ]
    return usage


async def test_windows_are_today_in_utc_and_the_last_7_and_30_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admin reads "today" as a calendar day; the week and month match the
    rolling windows of the user block above it."""
    monkeypatch.setattr(get_llm_usage_stats, "get_utc_now", lambda: NOW)
    session = FakeAsyncSession()
    usage = _usage()

    stats = await GetLLMUsageStatsUseCase(
        SQLAlchemyUnitOfWork(session), usage
    ).execute()

    month = NOW - timedelta(days=30)
    assert [call.args for call in usage.summary.await_args_list] == [
        (session, datetime(2026, 1, 8, tzinfo=UTC), NOW),
        (session, NOW - timedelta(days=7), NOW),
        (session, month, NOW),
    ]
    usage.by_purpose.assert_awaited_once_with(session, month, NOW)
    usage.top_users.assert_awaited_once_with(session, month, NOW, limit=TOP_USERS_LIMIT)
    assert stats == LLMUsageStats(
        today=EMPTY,
        week=EMPTY,
        month=EMPTY,
        by_purpose=usage.by_purpose.return_value,
        top_users=usage.top_users.return_value,
    )


async def test_the_read_is_capped_and_never_commits() -> None:
    """An admin command must not hold a pool connection for the session-wide
    statement timeout, and a read leaves nothing to commit."""
    session = FakeAsyncSession()
    await GetLLMUsageStatsUseCase(SQLAlchemyUnitOfWork(session), _usage()).execute()

    [timeout] = session.execute.await_args_list
    statement = str(timeout.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "set_config('statement_timeout', '10000', true)" in statement
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()
