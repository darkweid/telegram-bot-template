from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database.base import Base
from src.core.database.mixins import UUID7IDMixin
from src.core.llm.types import PURPOSE_MAX_LENGTH


class LLMUsageRecord(UUID7IDMixin, Base):
    """One provider call, successful or not: who, what for, how many tokens,
    what it cost. Counts only - never the prompt or the answer."""

    __tablename__ = "llm_usage"
    __table_args__ = (
        # Every reader asks for a time range: the /stats windows and the purge.
        Index("ix_llm_usage_created_at", "created_at"),
    )

    # Stamped by the recorder from the application clock: the /stats windows
    # are computed from the same clock.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # No foreign key: deleting a user must not delete what their calls cost,
    # and a call made by a worker task has no user at all.
    user_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    purpose: Mapped[str] = mapped_column(String(PURPOSE_MAX_LENGTH))
    # Plain strings, not PostgreSQL enums: a new provider or status must not
    # need an ALTER TYPE migration on an append-only table.
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cache_read_tokens: Mapped[int] = mapped_column(default=0)
    cache_write_tokens: Mapped[int] = mapped_column(default=0)
    # NULL when the price is unknown, which is not the same as free. Eight
    # places, as COST_QUANTUM: one cache-read token of the cheapest priced
    # model costs a hundred-millionth of a dollar.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), default=None)
    latency_ms: Mapped[int]
    status: Mapped[str] = mapped_column(String(16))
    # The class name of the LLMError, never its message.
    error_type: Mapped[str | None] = mapped_column(String(128), default=None)
