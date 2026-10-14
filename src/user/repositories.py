from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Boolean, func, literal_column, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database.repositories import BaseRepository
from src.user.models import User
from src.user.schemas import TelegramProfile


@dataclass(frozen=True, slots=True)
class UpsertedUser:
    """The stored user and whether this call inserted it (`created`) or updated it."""

    user: User
    created: bool


class UserRepository(BaseRepository[User]):
    model = User

    async def upsert_from_telegram(
        self, session: AsyncSession, profile: TelegramProfile, now: datetime
    ) -> UpsertedUser:
        """Insert the user or refresh their profile, in one statement.

        One statement rather than a read and a write: two updates of a new
        user that arrive together (a message and a callback in different
        chats) would otherwise both try the INSERT. The stored `locale` is
        never touched here, and the block flag is cleared: Telegram delivered
        an update from this user, so the bot is not blocked.
        """
        values = {
            "id": profile.id,
            "first_name": profile.first_name,
            "username": profile.username,
            "last_name": profile.last_name,
            "language_code": profile.language_code,
            "last_seen_at": now,
            "is_blocked": False,
            "blocked_at": None,
        }
        insertion = insert(User).values(**values)
        statement = insertion.on_conflict_do_update(
            index_elements=[User.id],
            set_={
                **{
                    column: insertion.excluded[column]
                    for column in values
                    if column != "id"
                },
                # onupdate defaults do not apply to ON CONFLICT DO UPDATE.
                "updated_at": func.now(),
            },
        ).returning(
            User,
            # xmax is 0 only for a row this statement inserted; an updated row
            # carries the id of the transaction that replaced its old version.
            literal_column("xmax = 0", Boolean).label("created"),
        )
        row = (
            await session.execute(
                statement, execution_options={"populate_existing": True}
            )
        ).one()
        return UpsertedUser(user=row[0], created=bool(row.created))

    async def set_blocked(
        self, session: AsyncSession, user_id: int, blocked: bool, now: datetime
    ) -> bool:
        """Record that the user blocked (or unblocked) the bot.

        `blocked_at` keeps the first time: a broadcast that hits the same
        blocked user twice must not move it.
        """
        statement = (
            update(User)
            .where(User.id == user_id)
            .values(
                is_blocked=blocked,
                blocked_at=func.coalesce(User.blocked_at, now) if blocked else None,
            )
            .returning(User.id)
        )
        return (await session.execute(statement)).scalar_one_or_none() is not None

    async def set_locale(
        self, session: AsyncSession, user_id: int, locale: str
    ) -> bool:
        statement = (
            update(User)
            .where(User.id == user_id)
            .values(locale=locale)
            .returning(User.id)
        )
        return (await session.execute(statement)).scalar_one_or_none() is not None
