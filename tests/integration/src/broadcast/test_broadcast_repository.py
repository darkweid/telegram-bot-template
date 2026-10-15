from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.broadcast.enums import BroadcastStatus
from src.broadcast.models import Broadcast
from src.broadcast.repositories import BroadcastRepository
from src.broadcast.schemas import Checkpoint
from src.broadcast.services import STALE_AFTER
from src.user.models import User
from tests.integration.conftest import build_user_data, new_user_id

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
CHECKPOINT = Checkpoint(cursor_user_id=5, sent=1, failed=0, unreachable=0)


async def _broadcast(session: AsyncSession, **overrides: Any) -> Broadcast:
    admin_id = new_user_id()
    session.add(User(**build_user_data(admin_id)))
    # Flushed first: without a relationship the unit of work does not order
    # the two inserts by the foreign key.
    await session.flush()
    row = Broadcast(
        **(
            {
                "admin_id": admin_id,
                "source_chat_id": admin_id,
                "source_message_id": 1,
                "progress_message_id": 2,
                "total": 10,
            }
            | overrides
        )
    )
    session.add(row)
    await session.flush()
    return row


async def test_a_broadcast_is_claimed_once(db_session: AsyncSession) -> None:
    """The same task delivered twice must not start two parallel runs."""
    broadcasts = BroadcastRepository()
    row = await _broadcast(db_session)
    claimed = await broadcasts.claim_run(
        db_session, row.id, uuid4(), now=NOW, stale_before=NOW - STALE_AFTER
    )
    assert claimed is not None
    assert (claimed.status, claimed.started_at) == (BroadcastStatus.RUNNING, NOW)
    again = await broadcasts.claim_run(
        db_session, row.id, uuid4(), now=NOW, stale_before=NOW - STALE_AFTER
    )
    assert again is None


async def test_a_stale_run_is_taken_over_and_loses_its_lease(
    db_session: AsyncSession,
) -> None:
    """A worker that died leaves a lease nobody renews; the next run takes the
    broadcast, and the old run, if it was only slow, must stop writing."""
    broadcasts = BroadcastRepository()
    row = await _broadcast(db_session)
    old_run, new_run = uuid4(), uuid4()
    await broadcasts.claim_run(
        db_session, row.id, old_run, now=NOW, stale_before=NOW - STALE_AFTER
    )
    later = NOW + timedelta(minutes=10)
    taken = await broadcasts.claim_run(
        db_session, row.id, new_run, now=later, stale_before=later - STALE_AFTER
    )
    assert taken is not None
    assert (taken.run_id, taken.started_at) == (new_run, NOW)
    lost = await broadcasts.save_checkpoint(
        db_session, row.id, old_run, checkpoint=CHECKPOINT, now=later
    )
    assert lost is None
    kept = await broadcasts.save_checkpoint(
        db_session, row.id, new_run, checkpoint=CHECKPOINT, now=later
    )
    assert kept is False


async def test_a_stop_reaches_the_run_and_ends_with_the_broadcast(
    db_session: AsyncSession,
) -> None:
    """The Stop button only sets a flag; the run reads it at its checkpoint.
    A finished broadcast has nothing left to stop."""
    broadcasts = BroadcastRepository()
    row = await _broadcast(db_session)
    run_id = uuid4()
    await broadcasts.claim_run(
        db_session, row.id, run_id, now=NOW, stale_before=NOW - STALE_AFTER
    )
    assert await broadcasts.request_stop(db_session, row.id) is True
    stop = await broadcasts.save_checkpoint(
        db_session, row.id, run_id, checkpoint=CHECKPOINT, now=NOW
    )
    assert stop is True
    finished = await broadcasts.finish(
        db_session,
        row.id,
        run_id,
        checkpoint=CHECKPOINT,
        status=BroadcastStatus.CANCELLED,
        now=NOW,
    )
    assert finished is True
    assert await broadcasts.request_stop(db_session, row.id) is False
    await db_session.refresh(row)
    assert (row.status, row.sent, row.cursor_user_id, row.finished_at) == (
        BroadcastStatus.CANCELLED,
        1,
        5,
        NOW,
    )


@pytest.mark.parametrize(
    ("status", "total"),
    [(BroadcastStatus.COMPLETED, 4), (BroadcastStatus.CANCELLED, 10)],
)
async def test_a_completed_broadcast_totals_the_users_it_walked(
    db_session: AsyncSession, status: BroadcastStatus, total: int
) -> None:
    """Recipients are counted at start and the walk is the truth: users who
    turned reachable or unreachable mid-run change who is walked. A stopped
    broadcast keeps the count it was started with, to show how far it got."""
    broadcasts = BroadcastRepository()
    row = await _broadcast(db_session)
    run_id = uuid4()
    await broadcasts.claim_run(
        db_session, row.id, run_id, now=NOW, stale_before=NOW - STALE_AFTER
    )
    finished = await broadcasts.finish(
        db_session,
        row.id,
        run_id,
        checkpoint=Checkpoint(cursor_user_id=9, sent=2, failed=1, unreachable=1),
        status=status,
        now=NOW,
    )
    assert finished is True
    await db_session.refresh(row)
    assert (row.status, row.total) == (status, total)


async def test_stalled_broadcasts_are_found_and_live_ones_are_not(
    db_session: AsyncSession,
) -> None:
    """The resume task restarts what a dead worker left behind; a live run
    must never get a second worker."""
    broadcasts = BroadcastRepository()
    fresh = await _broadcast(db_session, created_at=NOW)
    lost = await _broadcast(db_session, created_at=NOW - timedelta(hours=1))
    live = await _broadcast(
        db_session, status=BroadcastStatus.RUNNING, run_id=uuid4(), checkpoint_at=NOW
    )
    dead = await _broadcast(
        db_session,
        status=BroadcastStatus.RUNNING,
        run_id=uuid4(),
        checkpoint_at=NOW - timedelta(hours=1),
    )
    done = await _broadcast(
        db_session,
        status=BroadcastStatus.COMPLETED,
        checkpoint_at=NOW - timedelta(hours=1),
        created_at=NOW - timedelta(hours=1),
    )
    assert await broadcasts.has_active(db_session)
    stalled = set(await broadcasts.find_stalled(db_session, NOW - STALE_AFTER))
    assert {lost.id, dead.id} <= stalled
    assert {fresh.id, live.id, done.id}.isdisjoint(stalled)


async def test_finished_unreported_broadcasts_are_found_once_their_run_is_silent(
    db_session: AsyncSession,
) -> None:
    """The run that finished gets STALE_AFTER to report; after that the resume
    task sends the report, and never for one already sent or still running."""
    broadcasts = BroadcastRepository()
    old = NOW - timedelta(hours=1)
    unreported = {
        status: await _broadcast(
            db_session, status=status, run_id=uuid4(), checkpoint_at=old
        )
        for status in (
            BroadcastStatus.COMPLETED,
            BroadcastStatus.CANCELLED,
            BroadcastStatus.FAILED,
        )
    }
    just_finished = await _broadcast(
        db_session, status=BroadcastStatus.COMPLETED, run_id=uuid4(), checkpoint_at=NOW
    )
    reported = await _broadcast(
        db_session,
        status=BroadcastStatus.COMPLETED,
        run_id=uuid4(),
        checkpoint_at=old,
        reported_at=old,
    )
    dead = await _broadcast(
        db_session, status=BroadcastStatus.RUNNING, run_id=uuid4(), checkpoint_at=old
    )
    found = set(await broadcasts.find_unreported(db_session, NOW - STALE_AFTER))
    assert {row.id for row in unreported.values()} <= found
    assert {just_finished.id, reported.id, dead.id}.isdisjoint(found)


async def test_a_report_is_claimed_once_and_recorded_by_its_owner(
    db_session: AsyncSession,
) -> None:
    """Two resume rounds queue two runs; only one may send the report, and a
    run that lost the claim must not record it."""
    broadcasts = BroadcastRepository()
    old = NOW - timedelta(hours=1)
    row = await _broadcast(
        db_session, status=BroadcastStatus.COMPLETED, run_id=uuid4(), checkpoint_at=old
    )
    first, second = uuid4(), uuid4()
    claimed = await broadcasts.claim_report(
        db_session, row.id, first, now=NOW, stale_before=NOW - STALE_AFTER
    )
    assert claimed is not None
    assert (claimed.run_id, claimed.checkpoint_at) == (first, NOW)
    again = await broadcasts.claim_report(
        db_session, row.id, second, now=NOW, stale_before=NOW - STALE_AFTER
    )
    assert again is None
    assert await broadcasts.mark_reported(db_session, row.id, second, now=NOW) is False
    assert await broadcasts.mark_reported(db_session, row.id, first, now=NOW) is True
    later = NOW + timedelta(hours=1)
    assert (
        await broadcasts.claim_report(
            db_session, row.id, second, now=later, stale_before=later - STALE_AFTER
        )
        is None
    )
    await db_session.refresh(row)
    assert row.reported_at == NOW


async def test_a_running_broadcast_has_no_report_to_claim(
    db_session: AsyncSession,
) -> None:
    """A dead run is resumed by claim_run; claiming its report instead would
    announce a broadcast that has not finished."""
    broadcasts = BroadcastRepository()
    old = NOW - timedelta(hours=1)
    row = await _broadcast(
        db_session, status=BroadcastStatus.RUNNING, run_id=uuid4(), checkpoint_at=old
    )
    assert (
        await broadcasts.claim_report(
            db_session, row.id, uuid4(), now=NOW, stale_before=NOW - STALE_AFTER
        )
        is None
    )


async def test_the_run_that_finished_records_its_report(
    db_session: AsyncSession,
) -> None:
    """finish keeps the run's lease, so the normal path records the report
    without a claim of its own."""
    broadcasts = BroadcastRepository()
    row = await _broadcast(db_session)
    run_id = uuid4()
    await broadcasts.claim_run(
        db_session, row.id, run_id, now=NOW, stale_before=NOW - STALE_AFTER
    )
    await broadcasts.finish(
        db_session,
        row.id,
        run_id,
        checkpoint=CHECKPOINT,
        status=BroadcastStatus.COMPLETED,
        now=NOW,
    )
    assert await broadcasts.mark_reported(db_session, row.id, run_id, now=NOW) is True
    assert await broadcasts.mark_reported(db_session, row.id, run_id, now=NOW) is False
