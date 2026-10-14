from datetime import datetime
from uuid import UUID as PY_UUID

from sqlalchemy import DateTime, Integer, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
import uuid6


class TimestampMixin:
    """created_at/updated_at stamped by the database clock (func.now())."""

    __abstract__ = True

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class UUID7IDMixin:
    """Time-ordered UUID v7 primary key.

    Inserts stay clustered in the index; the id reveals its creation time.
    """

    __abstract__ = True

    id: Mapped[PY_UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid6.uuid7
    )


class IntegerIDMixin:
    __abstract__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


class SoftDeleteMixin:
    __abstract__ = True

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    is_deleted: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)
