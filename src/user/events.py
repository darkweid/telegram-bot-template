from dataclasses import dataclass
from typing import ClassVar

from src.core.events.schemas import DomainEvent


@dataclass(frozen=True, slots=True)
class UserRegistered(DomainEvent):
    """A Telegram user reached the bot for the first time."""

    code: ClassVar[str] = "user.registered"

    user_id: int
