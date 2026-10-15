from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import (
    Boolean,
    ColumnElement,
    func,
    literal_column,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database.repositories import BaseRepository
from src.user.models import User
from src.user.schemas import TelegramProfile, UserStats


def _recipient_filter(registered_by: datetime | None) -> list[ColumnElement[bool]]:
    """Who gets a broadcast: reachable, not banned, and present by `registered_by`."""
    conditions: list[ColumnElement[bool]] = [
        User.is_reachable.expression,
        ~User.is_banned,
    ]
    if registered_by is not None:
        conditions.append(User.created_at <= registered_by)
    return conditions


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
        never touched here, and the user is marked reachable: Telegram delivered
        an update from this user, so the bot can still reach them.
        """
        values = {
            "id": profile.id,
            "first_name": profile.first_name,
            "username": profile.username,
            "last_name": profile.last_name,
            "language_code": profile.language_code,
            "last_seen_at": now,
            "is_reachable": True,
            "unreachable_since": None,
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

    async def set_reachable(
        self, session: AsyncSession, user_id: int, reachable: bool, now: datetime
    ) -> bool:
        """Record whether the bot can still deliver messages to the user.

        `unreachable_since` keeps the first time: a broadcast that hits the
        same unreachable user twice must not move it.
        """
        statement = (
            update(User)
            .where(User.id == user_id)
            .values(
                is_reachable=reachable,
                unreachable_since=(
                    None if reachable else func.coalesce(User.unreachable_since, now)
                ),
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

    async def set_banned(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        banned: bool,
        by: int | None,
        now: datetime,
    ) -> bool:
        """Whether this call changed the user: False when the row is missing or
        already in that state.

        The state is part of the WHERE: of two admins banning at once, the
        second waits on the row lock, re-checks the condition and changes
        nothing, so only one is told they banned the user.
        """
        statement = (
            update(User)
            .where(User.id == user_id, User.is_banned.is_distinct_from(banned))
            .values(is_banned=banned, banned_at=now if banned else None, banned_by=by)
            .returning(User.id)
        )
        return (await session.execute(statement)).scalar_one_or_none() is not None

    async def find_by_username(
        self, session: AsyncSession, username: str
    ) -> Sequence[User]:
        """Every stored user with this username, ignoring case. More than one
        when a username changed hands and the old owner's row is stale.

        The comparison is written as `lower(username)` so that
        `ix_users_lower_username` serves it."""
        statement = select(User).where(func.lower(User.username) == username.lower())
        return (await session.execute(statement)).scalars().all()

    async def get_stats(self, session: AsyncSession, now: datetime) -> UserStats:
        """Every count of /stats in one pass over the table, so they agree
        with each other."""
        day, week, month = (now - timedelta(days=days) for days in (1, 7, 30))
        statement = select(
            func.count().label("total"),
            func.count().filter(User.is_reachable).label("reachable"),
            func.count().filter(User.is_banned).label("banned"),
            func.count().filter(User.last_seen_at >= day).label("active_day"),
            func.count().filter(User.last_seen_at >= week).label("active_week"),
            func.count().filter(User.last_seen_at >= month).label("active_month"),
            func.count().filter(User.created_at >= day).label("new_day"),
            func.count().filter(User.created_at >= week).label("new_week"),
            func.count().filter(User.created_at >= month).label("new_month"),
        ).select_from(User)
        row = (await session.execute(statement)).one()
        return UserStats(**row._mapping)

    async def get_recipient_ids(
        self,
        session: AsyncSession,
        *,
        after: int,
        limit: int,
        registered_by: datetime | None = None,
    ) -> list[int]:
        """The next broadcast recipients after `after`, in id order.

        The predicate is the one of the partial index ix_users_recipients, so
        each batch is an index range scan. `registered_by` leaves out users
        created later.
        """
        query = (
            select(User.id)
            .where(*_recipient_filter(registered_by), User.id > after)
            .order_by(User.id)
            .limit(limit)
        )
        return list((await session.scalars(query)).all())

    async def count_recipients(
        self, session: AsyncSession, *, registered_by: datetime | None = None
    ) -> int:
        query = (
            select(func.count())
            .select_from(User)
            .where(*_recipient_filter(registered_by))
        )
        return (await session.execute(query)).scalar_one()
