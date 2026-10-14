from datetime import datetime, timedelta
import re

from src.user.schemas import TelegramProfile, UserProfile

# What Telegram accepts as a username: 5 to 32 letters, digits, underscores.
_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")


# A user named in a command: a Telegram id, or a username without the "@".
type UserReference = int | str


def needs_write(
    stored: UserProfile | None,
    profile: TelegramProfile,
    now: datetime,
    interval: timedelta,
) -> bool:
    """Whether this update must reach the database.

    Every update reads the stored user; only some write. A busy chat would
    otherwise rewrite the same row on every message just to move
    `last_seen_at` by a second, so the stamp is refreshed at most once per
    `interval` unless something real changed.
    """
    if stored is None or not stored.is_reachable or stored.differs_from(profile):
        return True
    return now - stored.last_seen_at >= interval


def parse_user_reference(text: str | None) -> UserReference | None:
    """`123456789`, `@username` or `username`; None for anything else."""
    value = (text or "").strip()
    if value.isdecimal():
        user_id = int(value)
        return user_id if user_id > 0 else None
    username = value.removeprefix("@")
    return username if _USERNAME.fullmatch(username) else None
