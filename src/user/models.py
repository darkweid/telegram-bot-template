from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Text, false, text, true
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database.base import Base
from src.core.database.mixins import TimestampMixin


class User(TimestampMixin, Base):
    """A Telegram user who has talked to the bot in a private chat.

    The primary key is the Telegram user id: it is stable, unique and what
    every update carries, so there is nothing to map. Profile fields mirror
    Telegram and are refreshed when they change; `locale` is the user's own
    choice from /language and wins over `language_code`. `is_banned` is set
    by an admin; a banned user's updates are dropped unless they hold a role.
    """

    __tablename__ = "users"
    __table_args__ = (
        # A broadcast walks its recipients in id order (`WHERE is_reachable AND
        # NOT is_banned AND id > :cursor`). A partial index on id serves that
        # keyset scan directly; an index on the boolean columns would not.
        Index(
            "ix_users_recipients",
            "id",
            postgresql_where=text("is_reachable AND NOT is_banned"),
        ),
    )

    # Telegram ids exceed 2**31 and are assigned by Telegram, never by us.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(Text)
    first_name: Mapped[str] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    # An IETF tag as Telegram sends it ("en", "pt-br"); it may name a language
    # we have no catalog for, so it is stored as given and resolved on use.
    language_code: Mapped[str | None] = mapped_column(Text)
    locale: Mapped[str | None] = mapped_column(Text)
    is_reachable: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    unreachable_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_banned: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The admin's Telegram id; kept after unban as the last one who touched it.
    banned_by: Mapped[int | None] = mapped_column(BigInteger)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
