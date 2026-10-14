from datetime import datetime, timedelta

from src.user.schemas import TelegramProfile, UserProfile


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
