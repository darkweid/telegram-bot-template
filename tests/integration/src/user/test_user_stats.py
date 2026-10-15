from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.user.repositories import UserRepository
from tests.factories.users import build_user_model
from tests.integration.conftest import new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


async def test_stats_count_each_window_once(db_session: AsyncSession) -> None:
    """The integration database is shared with the other tests, so the counts
    are compared as deltas: three users go in, each must land in exactly the
    windows its timestamps put it in."""
    users = UserRepository()
    before = await users.get_stats(db_session, NOW)
    db_session.add_all(
        [
            build_user_model(
                id=new_user_id(),
                created_at=NOW - timedelta(hours=2),
                last_seen_at=NOW - timedelta(hours=2),
            ),
            build_user_model(
                id=new_user_id(),
                created_at=NOW - timedelta(days=20),
                last_seen_at=NOW - timedelta(days=3),
                is_reachable=False,
                unreachable_since=NOW - timedelta(days=3),
            ),
            build_user_model(
                id=new_user_id(),
                created_at=NOW - timedelta(days=60),
                last_seen_at=NOW - timedelta(days=60),
                is_banned=True,
                banned_at=NOW - timedelta(days=59),
                banned_by=1,
            ),
        ]
    )
    await db_session.flush()
    after = await users.get_stats(db_session, NOW)

    assert (after.total - before.total, after.reachable - before.reachable) == (3, 2)
    assert after.banned - before.banned == 1
    assert (
        after.active_day - before.active_day,
        after.active_week - before.active_week,
        after.active_month - before.active_month,
    ) == (1, 2, 2)
    assert (
        after.new_day - before.new_day,
        after.new_week - before.new_week,
        after.new_month - before.new_month,
    ) == (1, 1, 2)
