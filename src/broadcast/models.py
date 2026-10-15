from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.broadcast.enums import BroadcastStatus
from src.core.database.base import Base
from src.core.database.mixins import TimestampMixin, UUID7IDMixin


class Broadcast(UUID7IDMixin, TimestampMixin, Base):
    """An admin's message copied to every reachable, unbanned user.

    The message stays in the admin's chat and is copied from there
    (`source_chat_id`, `source_message_id`). `cursor_user_id` and the counters
    are the last checkpoint: a restarted run continues after the cursor.
    `run_id` and `checkpoint_at` are the lease of the run that owns the
    broadcast; a lease not renewed for STALE_AFTER may be taken over.
    `reported_at` is set once the final report has gone out; until then a
    finished broadcast is claimed again, through the same lease, to resend it.
    """

    __tablename__ = "broadcasts"

    admin_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(Integer)
    # The admin's message that shows progress and carries the Stop button.
    progress_message_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[BroadcastStatus] = mapped_column(
        SQLEnum(BroadcastStatus), default=BroadcastStatus.PENDING
    )
    total: Mapped[int] = mapped_column(Integer)
    sent: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    unreachable: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    # Telegram user ids are positive, so 0 comes before the first of them.
    cursor_user_id: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    stop_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
