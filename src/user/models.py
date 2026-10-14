from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database.base import Base
from src.core.database.mixins import TimestampMixin


class User(TimestampMixin, Base):
    """A Telegram user who has talked to the bot in a private chat.

    The primary key is the Telegram user id: it is stable, unique and what
    every update carries, so there is nothing to map. Profile fields mirror
    Telegram and are refreshed when they change; `locale` is the user's own
    choice from /language and wins over `language_code`.
    """

    __tablename__ = "users"

    # Telegram ids exceed 2**31 and are assigned by Telegram, never by us.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(Text)
    first_name: Mapped[str] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    # An IETF tag as Telegram sends it ("en", "pt-br"); it may name a language
    # we have no catalog for, so it is stored as given and resolved on use.
    language_code: Mapped[str | None] = mapped_column(Text)
    locale: Mapped[str | None] = mapped_column(Text)
    is_blocked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), index=True
    )
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
