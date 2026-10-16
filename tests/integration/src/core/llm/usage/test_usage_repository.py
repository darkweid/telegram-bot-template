from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.core.llm.enums import LLMCallStatus, LLMProviderName
from src.core.llm.usage.models import LLMUsageRecord
from src.core.llm.usage.repository import (
    LLMUsageByPurpose,
    LLMUsageRepository,
    LLMUsageSummary,
    LLMUsageTopUser,
)
from tests.integration.conftest import new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")

# A window nobody else writes into: db_session rolls back, and the only test
# that commits llm_usage rows (test_recorder.py) writes in 2002.
SINCE = datetime(2001, 1, 1, tzinfo=UTC)
UNTIL = SINCE + timedelta(days=1)


async def _add(
    session: AsyncSession, created_at: datetime, **overrides: Any
) -> LLMUsageRecord:
    data: dict[str, Any] = {
        "created_at": created_at,
        "user_id": None,
        "purpose": "note_summary",
        "provider": LLMProviderName.ANTHROPIC,
        "model": "claude-sonnet-5-5",
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": Decimal("0.00100000"),
        "latency_ms": 500,
        "status": LLMCallStatus.SUCCESS,
    }
    row = await LLMUsageRepository().create(session, data | overrides)
    await session.flush()
    return row


async def test_the_summary_counts_errors_and_unpriced_calls_apart(
    db_session: AsyncSession,
) -> None:
    """A sum that silently skipped unpriced calls would read as a cheaper
    month than it was."""
    await _add(db_session, SINCE, input_tokens=100, cache_read_tokens=50)
    await _add(db_session, SINCE + timedelta(hours=1), cost_usd=None)
    await _add(
        db_session,
        SINCE + timedelta(hours=2),
        status=LLMCallStatus.ERROR,
        error_type="LLMTimeout",
        input_tokens=0,
        output_tokens=0,
        cost_usd=None,
    )
    # A failed repair: billed, but the model has no known price.
    await _add(
        db_session,
        SINCE + timedelta(hours=3),
        status=LLMCallStatus.ERROR,
        error_type="LLMOutputError",
        input_tokens=50,
        output_tokens=5,
        cost_usd=None,
    )
    summary = await LLMUsageRepository().summary(db_session, SINCE, UNTIL)
    assert summary == LLMUsageSummary(
        calls=4,
        errors=2,
        input_tokens=250,
        output_tokens=25,
        cache_read_tokens=50,
        cache_write_tokens=0,
        cost_usd=Decimal("0.00100000"),
        unpriced_calls=2,
    )


async def test_the_window_is_half_open(db_session: AsyncSession) -> None:
    """`today` and `yesterday` share their boundary; a call exactly at
    midnight must be counted once."""
    await _add(db_session, SINCE - timedelta(microseconds=1))
    await _add(db_session, SINCE)
    await _add(db_session, UNTIL)
    summary = await LLMUsageRepository().summary(db_session, SINCE, UNTIL)
    assert summary.calls == 1


async def test_an_empty_window_sums_to_zero(db_session: AsyncSession) -> None:
    """/stats renders a quiet day, not a crash on NULL sums."""
    summary = await LLMUsageRepository().summary(
        db_session, datetime(2000, 6, 1, tzinfo=UTC), datetime(2000, 6, 2, tzinfo=UTC)
    )
    assert summary == LLMUsageSummary(0, 0, 0, 0, 0, 0, Decimal(0), 0)


async def test_purposes_come_most_expensive_first(db_session: AsyncSession) -> None:
    await _add(db_session, SINCE, purpose="cheap", cost_usd=Decimal("0.001"))
    await _add(db_session, SINCE, purpose="cheap", cost_usd=Decimal("0.001"))
    await _add(db_session, SINCE, purpose="dear", cost_usd=Decimal("0.5"))
    await _add(db_session, SINCE, purpose="unpriced", cost_usd=None)
    rows = await LLMUsageRepository().by_purpose(db_session, SINCE, UNTIL)
    assert rows == [
        LLMUsageByPurpose("dear", 1, Decimal("0.50000000")),
        LLMUsageByPurpose("cheap", 2, Decimal("0.00200000")),
        LLMUsageByPurpose("unpriced", 1, Decimal(0)),
    ]


async def test_top_users_skip_calls_without_a_user(db_session: AsyncSession) -> None:
    """Worker tasks have no user; a NULL row in the top would be noise."""
    heavy, light = new_user_id(), new_user_id()
    await _add(db_session, SINCE, user_id=heavy, cost_usd=Decimal("0.3"))
    await _add(db_session, SINCE, user_id=heavy, cost_usd=Decimal("0.3"))
    await _add(db_session, SINCE, user_id=light, cost_usd=Decimal("0.1"))
    await _add(db_session, SINCE, user_id=None, cost_usd=Decimal("9"))
    repository = LLMUsageRepository()
    assert await repository.top_users(db_session, SINCE, UNTIL, limit=5) == [
        LLMUsageTopUser(heavy, 2, Decimal("0.60000000")),
        LLMUsageTopUser(light, 1, Decimal("0.10000000")),
    ]
    assert await repository.top_users(db_session, SINCE, UNTIL, limit=1) == [
        LLMUsageTopUser(heavy, 2, Decimal("0.60000000")),
    ]


async def test_the_purge_takes_old_rows_in_batches(db_session: AsyncSession) -> None:
    """Retention must not delete the window /stats still shows, and one
    batch is bounded so a first purge after months never runs into the
    statement timeout."""
    cutoff = SINCE + timedelta(hours=12)
    for hours in range(3):
        await _add(db_session, SINCE + timedelta(hours=hours))
    kept = await _add(db_session, cutoff)
    repository = LLMUsageRepository()
    assert await repository.purge_older_than(db_session, cutoff, limit=2) == 2
    assert await repository.purge_older_than(db_session, cutoff, limit=2) == 1
    assert await repository.purge_older_than(db_session, cutoff, limit=2) == 0
    remaining = (
        await db_session.scalars(
            select(LLMUsageRecord.id).where(
                LLMUsageRecord.created_at >= SINCE, LLMUsageRecord.created_at < UNTIL
            )
        )
    ).all()
    assert remaining == [kept.id]


async def test_llm_usage_is_indexed_by_time(integration_engine: AsyncEngine) -> None:
    """Every read is a time range; without the index /stats scans months of
    rows, and nothing fails to say so."""
    async with integration_engine.connect() as connection:
        definition = (
            await connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE tablename = 'llm_usage'"
                    " AND indexname = 'ix_llm_usage_created_at'"
                )
            )
        ).scalar_one()
    assert definition.endswith("(created_at)")
