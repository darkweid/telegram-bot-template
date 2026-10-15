from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database.repositories import BaseRepository
from src.core.outbox.enums import OutboxMessageStatus
from src.core.outbox.models import OutboxMessage
from src.core.utils.datetime_utils import get_utc_now

LAST_ERROR_MAX_LENGTH = 2000


class OutboxRepository(BaseRepository[OutboxMessage]):
    model = OutboxMessage

    async def get_batch_for_publish(
        self,
        session: AsyncSession,
        limit: int,
        *,
        min_age: timedelta,
        max_age: timedelta,
    ) -> Sequence[OutboxMessage]:
        """
        Lock a FIFO batch of pending rows older than `min_age` and younger than
        `max_age`; SKIP LOCKED keeps concurrent sweeps disjoint.

        A younger row may still be in its after-commit publish, which sends the
        task and marks the row published in two steps: taking it here would
        publish it a second time. Age is measured on the database clock, the
        same one that stamped `created_at`, so app-server skew cannot shrink the
        window. A row past `max_age` is never sent again: `fail_expired` takes it.
        """
        query = (
            select(self.model)
            .where(
                self.model.status == OutboxMessageStatus.PENDING,
                self.model.created_at < func.now() - min_age,
                self.model.created_at >= func.now() - max_age,
            )
            .order_by(self.model.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(query)
        return result.scalars().all()

    async def fail_expired(
        self, session: AsyncSession, max_age: timedelta, error: str, *, limit: int
    ) -> Sequence[tuple[UUID, str]]:
        """Move at most `limit` pending rows created more than `max_age` ago to
        FAILED; returns the id and task name of each.

        The rows are locked with SKIP LOCKED like a publish batch, so a row in
        another sweep's batch is left to the next run. Age is measured on the
        database clock, as in `get_batch_for_publish`.
        """
        expired = (
            select(self.model.id)
            .where(
                self.model.status == OutboxMessageStatus.PENDING,
                self.model.created_at < func.now() - max_age,
            )
            .order_by(self.model.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(
            update(self.model)
            .where(self.model.id.in_(expired))
            .values(
                status=OutboxMessageStatus.FAILED,
                last_error=error[:LAST_ERROR_MAX_LENGTH],
            )
            .returning(self.model.id, self.model.task_name)
        )
        return [(row.id, row.task_name) for row in result]

    async def mark_published(self, session: AsyncSession, message_id: UUID) -> None:
        await session.execute(
            update(self.model)
            .where(self.model.id == message_id)
            .values(
                status=OutboxMessageStatus.PUBLISHED,
                published_at=get_utc_now(),
            )
        )

    async def mark_publish_failure(
        self,
        session: AsyncSession,
        message_id: UUID,
        error: str,
        final: bool,
        *,
        count_attempt: bool = True,
    ) -> None:
        values: dict[str, Any] = {"last_error": error[:LAST_ERROR_MAX_LENGTH]}
        if count_attempt:
            values["attempts"] = self.model.attempts + 1
        if final:
            values["status"] = OutboxMessageStatus.FAILED
        await session.execute(
            update(self.model).where(self.model.id == message_id).values(**values)
        )

    async def purge_published(
        self, session: AsyncSession, cutoff: datetime, *, limit: int
    ) -> int:
        """Delete at most `limit` rows published before `cutoff`; returns how
        many went. A caller loops until fewer than `limit` come back."""
        doomed = (
            select(self.model.id)
            .where(
                self.model.status == OutboxMessageStatus.PUBLISHED,
                self.model.published_at < cutoff,
            )
            .limit(limit)
        )
        result = await session.execute(
            delete(self.model).where(self.model.id.in_(doomed))
        )
        return int(result.rowcount) if hasattr(result, "rowcount") else 0

    async def count_pending(self, session: AsyncSession) -> int:
        return await self.count(session, status=OutboxMessageStatus.PENDING)

    async def oldest_pending_age(self, session: AsyncSession) -> timedelta | None:
        """Age of the oldest pending row on the database clock, or None.

        clock_timestamp(), not now(): now() is frozen at the start of the
        transaction and would make a row written in it look negative-aged."""
        query = select(func.clock_timestamp() - func.min(self.model.created_at)).where(
            self.model.status == OutboxMessageStatus.PENDING
        )
        return await session.scalar(query)
