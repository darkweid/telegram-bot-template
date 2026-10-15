from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Self


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Something that happened in a domain. A subclass is a frozen dataclass
    of ids and plain values and names itself with `code`, which logs use."""

    code: ClassVar[str]


class ActorType(StrEnum):
    TELEGRAM_USER = "telegram_user"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who caused an event: a Telegram user, or the bot itself (a task, the
    scheduler)."""

    type: ActorType
    id: int | None

    @classmethod
    def telegram_user(cls, user_id: int) -> Self:
        return cls(type=ActorType.TELEGRAM_USER, id=user_id)

    @classmethod
    def system(cls) -> Self:
        return cls(type=ActorType.SYSTEM, id=None)


@dataclass(frozen=True, slots=True)
class PublishedEvent:
    event: DomainEvent
    actor: Actor
    occurred_at: datetime
