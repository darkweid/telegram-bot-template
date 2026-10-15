from dataclasses import asdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.broadcast.enums import ACTIVE_STATUSES, FINISHED_STATUSES, BroadcastStatus
from src.broadcast.models import Broadcast
from src.broadcast.schemas import Checkpoint
from src.core.database.repositories import BaseRepository


class BroadcastRepository(BaseRepository[Broadcast]):
    model = Broadcast

    async def has_active(self, session: AsyncSession) -> bool:
        query = select(exists().where(Broadcast.status.in_(ACTIVE_STATUSES)))
        return bool(await session.scalar(query))

    async def claim_run(
        self,
        session: AsyncSession,
        broadcast_id: UUID,
        run_id: UUID,
        *,
        now: datetime,
        stale_before: datetime,
    ) -> Broadcast | None:
        """Make `run_id` the owner of the broadcast and return the row.

        Takes a pending broadcast, or a running one whose lease was last renewed
        before `stale_before`. None when it has finished or a live run owns it.
        """
        query = (
            update(Broadcast)
            .where(
                Broadcast.id == broadcast_id,
                or_(
                    Broadcast.status == BroadcastStatus.PENDING,
                    and_(
                        Broadcast.status == BroadcastStatus.RUNNING,
                        Broadcast.checkpoint_at < stale_before,
                    ),
                ),
            )
            .values(
                status=BroadcastStatus.RUNNING,
                run_id=run_id,
                checkpoint_at=now,
                started_at=func.coalesce(Broadcast.started_at, now),
            )
            .returning(Broadcast)
        )
        # populate_existing: the row may already sit in the session's identity
        # map, and the caller must see the values this UPDATE wrote.
        return await session.scalar(
            query, execution_options={"populate_existing": True}
        )

    async def save_checkpoint(
        self,
        session: AsyncSession,
        broadcast_id: UUID,
        run_id: UUID,
        *,
        checkpoint: Checkpoint,
        now: datetime,
    ) -> bool | None:
        """Store the run's progress and renew its lease.

        Returns whether an admin asked to stop, or None when `run_id` no longer
        owns the broadcast: another run took it over, and this one must quit.
        """
        query = (
            update(Broadcast)
            .where(
                Broadcast.id == broadcast_id,
                Broadcast.run_id == run_id,
                Broadcast.status == BroadcastStatus.RUNNING,
            )
            .values(**asdict(checkpoint), checkpoint_at=now)
            .returning(Broadcast.stop_requested)
        )
        return await session.scalar(query)

    async def finish(
        self,
        session: AsyncSession,
        broadcast_id: UUID,
        run_id: UUID,
        *,
        checkpoint: Checkpoint,
        status: BroadcastStatus,
        now: datetime,
    ) -> bool:
        """Close the broadcast with its final counts; False when the lease was lost.

        A completed broadcast's total becomes the number of users the walk
        handled: the recipients are counted at start, and a user who turns
        reachable or unreachable mid-run is walked or skipped all the same.
        """
        values = asdict(checkpoint) | {
            "status": status,
            "checkpoint_at": now,
            "finished_at": now,
        }
        if status == BroadcastStatus.COMPLETED:
            values["total"] = checkpoint.processed
        query = (
            update(Broadcast)
            .where(
                Broadcast.id == broadcast_id,
                Broadcast.run_id == run_id,
                Broadcast.status == BroadcastStatus.RUNNING,
            )
            .values(**values)
            .returning(Broadcast.id)
        )
        return await session.scalar(query) is not None

    async def claim_report(
        self,
        session: AsyncSession,
        broadcast_id: UUID,
        run_id: UUID,
        *,
        now: datetime,
        stale_before: datetime,
    ) -> Broadcast | None:
        """Make `run_id` the owner of a finished broadcast whose final report
        never went out, and return the row.

        Takes it only when its lease was last renewed before `stale_before`:
        `finish` renews it, so the run that finished gets that long to report,
        and a resent report holds it against a second resend meanwhile.
        """
        query = (
            update(Broadcast)
            .where(
                Broadcast.id == broadcast_id,
                Broadcast.status.in_(FINISHED_STATUSES),
                Broadcast.reported_at.is_(None),
                Broadcast.checkpoint_at < stale_before,
            )
            .values(run_id=run_id, checkpoint_at=now)
            .returning(Broadcast)
        )
        return await session.scalar(
            query, execution_options={"populate_existing": True}
        )

    async def mark_reported(
        self, session: AsyncSession, broadcast_id: UUID, run_id: UUID, *, now: datetime
    ) -> bool:
        """Record that the final report went out; False when `run_id` no longer
        owns the broadcast or it was already reported."""
        query = (
            update(Broadcast)
            .where(
                Broadcast.id == broadcast_id,
                Broadcast.run_id == run_id,
                Broadcast.status.in_(FINISHED_STATUSES),
                Broadcast.reported_at.is_(None),
            )
            .values(reported_at=now)
            .returning(Broadcast.id)
        )
        return await session.scalar(query) is not None

    async def request_stop(self, session: AsyncSession, broadcast_id: UUID) -> bool:
        """False when the broadcast has already finished."""
        query = (
            update(Broadcast)
            .where(Broadcast.id == broadcast_id, Broadcast.status.in_(ACTIVE_STATUSES))
            .values(stop_requested=True)
            .returning(Broadcast.id)
        )
        return await session.scalar(query) is not None

    async def find_stalled(
        self, session: AsyncSession, stale_before: datetime
    ) -> list[UUID]:
        """Broadcasts nobody works on: running with a lease last renewed before
        `stale_before`, or pending since then (their task was lost)."""
        query = (
            select(Broadcast.id)
            .where(
                or_(
                    and_(
                        Broadcast.status == BroadcastStatus.RUNNING,
                        Broadcast.checkpoint_at < stale_before,
                    ),
                    and_(
                        Broadcast.status == BroadcastStatus.PENDING,
                        Broadcast.created_at < stale_before,
                    ),
                )
            )
            .order_by(Broadcast.created_at)
        )
        return list((await session.scalars(query)).all())

    async def find_unreported(
        self, session: AsyncSession, stale_before: datetime
    ) -> list[UUID]:
        """Finished broadcasts whose final report never went out and whose
        last run has been silent since `stale_before` (its worker stopped
        between finishing and reporting, or the report met a network error)."""
        query = (
            select(Broadcast.id)
            .where(
                Broadcast.status.in_(FINISHED_STATUSES),
                Broadcast.reported_at.is_(None),
                Broadcast.checkpoint_at < stale_before,
            )
            .order_by(Broadcast.created_at)
        )
        return list((await session.scalars(query)).all())
