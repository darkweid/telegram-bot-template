from dataclasses import dataclass
from datetime import datetime
from typing import Self

from aiogram.types import User as TelegramUser

from src.user.models import User


@dataclass(frozen=True, slots=True)
class TelegramProfile:
    """The part of a Telegram user we store, as the latest update reports it."""

    id: int
    first_name: str
    username: str | None
    last_name: str | None
    language_code: str | None

    @classmethod
    def from_aiogram(cls, user: TelegramUser) -> Self:
        return cls(
            id=user.id,
            first_name=user.first_name,
            username=user.username,
            last_name=user.last_name,
            language_code=user.language_code,
        )


@dataclass(frozen=True, slots=True)
class UserProfile:
    """A stored user as handlers see it: a snapshot, detached from any session."""

    id: int
    first_name: str
    username: str | None
    last_name: str | None
    language_code: str | None
    locale: str | None
    is_reachable: bool
    is_banned: bool
    last_seen_at: datetime

    @classmethod
    def from_model(cls, user: User) -> Self:
        return cls(
            id=user.id,
            first_name=user.first_name,
            username=user.username,
            last_name=user.last_name,
            language_code=user.language_code,
            locale=user.locale,
            is_reachable=user.is_reachable,
            is_banned=user.is_banned,
            last_seen_at=user.last_seen_at,
        )

    def differs_from(self, profile: TelegramProfile) -> bool:
        """Whether Telegram now reports a profile field other than the stored one."""
        return (
            self.first_name,
            self.username,
            self.last_name,
            self.language_code,
        ) != (
            profile.first_name,
            profile.username,
            profile.last_name,
            profile.language_code,
        )


@dataclass(frozen=True, slots=True)
class BanChange:
    user: UserProfile
    # False when the user already was in the requested state.
    changed: bool


@dataclass(frozen=True, slots=True)
class UserStats:
    """User counts for /stats. `active_*` count by the last update seen,
    `new_*` by the first; the windows are the last 1, 7 and 30 days."""

    total: int
    reachable: int
    banned: int
    active_day: int
    active_week: int
    active_month: int
    new_day: int
    new_week: int
    new_month: int
