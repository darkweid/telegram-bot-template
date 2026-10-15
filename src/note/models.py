from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database.base import Base
from src.core.database.mixins import TimestampMixin, UUID7IDMixin

# Long enough for a real note, short enough that a whole note still fits in
# one Telegram message (4096 characters) with the delete prompt around it.
NOTE_TEXT_MAX_LENGTH = 2000


class Note(UUID7IDMixin, TimestampMixin, Base):
    """A short text a user keeps for themselves; only its author sees it.

    Deleted with its author: the foreign key cascades.
    """

    __tablename__ = "notes"
    __table_args__ = (
        # The service checks the length first to answer the user properly;
        # the constraint keeps a script or a later use case from storing an
        # empty or oversized note anyway.
        CheckConstraint(
            f"char_length(text) BETWEEN 1 AND {NOTE_TEXT_MAX_LENGTH}",
            name="text_length",
        ),
        # Every read is "this user's notes in id order": the list's keyset
        # and the latest notes. Led by user_id, it also serves the cascade
        # when a user row is deleted.
        Index("ix_notes_user_id_id", "user_id", "id"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    text: Mapped[str] = mapped_column(Text)
