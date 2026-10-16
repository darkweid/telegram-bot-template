from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database.repositories import BaseRepository
from src.core.llm.enums import LLMCallStatus
from src.core.llm.usage.models import LLMUsageRecord


@dataclass(frozen=True, slots=True)
class LLMUsageSummary:
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    # Over the calls whose price is known; `unpriced_calls` says how many
    # calls that used tokens (a failed repair included) the sum leaves out.
    cost_usd: Decimal
    unpriced_calls: int


@dataclass(frozen=True, slots=True)
class LLMUsageByPurpose:
    purpose: str
    calls: int
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class LLMUsageTopUser:
    user_id: int
    calls: int
    cost_usd: Decimal


class LLMUsageRepository(BaseRepository[LLMUsageRecord]):
    """Aggregates over `since <= created_at < until`, and the purge."""

    model = LLMUsageRecord

    def _window(self, since: datetime, until: datetime) -> ColumnElement[bool]:
        return (self.model.created_at >= since) & (self.model.created_at < until)

    def _cost(self) -> ColumnElement[Decimal | None]:
        return func.coalesce(func.sum(self.model.cost_usd), Decimal(0))

    async def summary(
        self, session: AsyncSession, since: datetime, until: datetime
    ) -> LLMUsageSummary:
        query = select(
            func.count().label("calls"),
            func.count()
            .filter(self.model.status == LLMCallStatus.ERROR)
            .label("errors"),
            func.coalesce(func.sum(self.model.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(self.model.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(self.model.cache_read_tokens), 0).label(
                "cache_read_tokens"
            ),
            func.coalesce(func.sum(self.model.cache_write_tokens), 0).label(
                "cache_write_tokens"
            ),
            self._cost().label("cost_usd"),
            func.count()
            .filter(
                self.model.cost_usd.is_(None),
                (self.model.input_tokens > 0)
                | (self.model.output_tokens > 0)
                | (self.model.cache_read_tokens > 0)
                | (self.model.cache_write_tokens > 0),
            )
            .label("unpriced_calls"),
        ).where(self._window(since, until))
        row = (await session.execute(query)).one()
        return LLMUsageSummary(
            calls=row.calls,
            errors=row.errors,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cache_read_tokens=row.cache_read_tokens,
            cache_write_tokens=row.cache_write_tokens,
            cost_usd=Decimal(row.cost_usd),
            unpriced_calls=row.unpriced_calls,
        )

    async def by_purpose(
        self, session: AsyncSession, since: datetime, until: datetime
    ) -> list[LLMUsageByPurpose]:
        """Most expensive first; ties by call count, then by name, so the order
        is stable between two reads of /stats."""
        calls = func.count().label("calls")
        cost = self._cost().label("cost_usd")
        query = (
            select(self.model.purpose, calls, cost)
            .where(self._window(since, until))
            .group_by(self.model.purpose)
            .order_by(cost.desc(), calls.desc(), self.model.purpose)
        )
        rows = (await session.execute(query)).all()
        return [
            LLMUsageByPurpose(
                purpose=row.purpose, calls=row.calls, cost_usd=Decimal(row.cost_usd)
            )
            for row in rows
        ]

    async def top_users(
        self, session: AsyncSession, since: datetime, until: datetime, *, limit: int
    ) -> list[LLMUsageTopUser]:
        """The `limit` users who cost the most; calls without a user (worker
        tasks) are left out."""
        calls = func.count().label("calls")
        cost = self._cost().label("cost_usd")
        query = (
            select(self.model.user_id, calls, cost)
            .where(self._window(since, until), self.model.user_id.is_not(None))
            .group_by(self.model.user_id)
            .order_by(cost.desc(), calls.desc(), self.model.user_id)
            .limit(limit)
        )
        rows = (await session.execute(query)).all()
        return [
            LLMUsageTopUser(
                user_id=row.user_id, calls=row.calls, cost_usd=Decimal(row.cost_usd)
            )
            for row in rows
        ]

    async def purge_older_than(
        self, session: AsyncSession, cutoff: datetime, *, limit: int
    ) -> int:
        """Delete at most `limit` rows created before `cutoff`; returns how
        many went. A caller loops until fewer than `limit` come back, so one
        transaction never holds locks on months of rows."""
        doomed = (
            select(self.model.id).where(self.model.created_at < cutoff).limit(limit)
        )
        result = await session.execute(
            delete(self.model).where(self.model.id.in_(doomed)).returning(self.model.id)
        )
        return len(result.scalars().all())
