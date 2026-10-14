from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.user.repositories import UserRepository
from src.user.schemas import UserStats
from src.user.usecases import get_user_stats
from src.user.usecases.get_user_stats import GetUserStatsUseCase
from tests.fakes.db import FakeAsyncSession

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
STATS = UserStats(
    total=3,
    reachable=2,
    banned=1,
    active_day=1,
    active_week=2,
    active_month=2,
    new_day=1,
    new_week=1,
    new_month=2,
)


async def test_stats_are_counted_from_now_under_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The windows are relative to the moment of the command, and a full pass
    over a large table must not hold a pool connection for the session-wide
    statement timeout."""
    monkeypatch.setattr(get_user_stats, "get_utc_now", lambda: NOW)
    session = FakeAsyncSession()
    users = AsyncMock(spec=UserRepository)
    users.get_stats.return_value = STATS

    result = await GetUserStatsUseCase(SQLAlchemyUnitOfWork(session), users).execute()

    assert result == STATS
    users.get_stats.assert_awaited_once_with(session, NOW)
    # Compiled with the values inlined: the plain form shows only bind names.
    [timeout] = session.execute.await_args_list
    statement = str(timeout.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "set_config('statement_timeout', '10000', true)" in statement


async def test_stats_never_commit() -> None:
    """A read-only use case leaves nothing to commit; the rollback also drops
    the transaction-local timeout."""
    session = FakeAsyncSession()
    users = AsyncMock(spec=UserRepository)
    users.get_stats.return_value = STATS
    await GetUserStatsUseCase(SQLAlchemyUnitOfWork(session), users).execute()
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()
