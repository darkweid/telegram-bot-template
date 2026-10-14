"""Stored users and their cached snapshots, for use case tests."""

from dataclasses import replace
from datetime import UTC, datetime

from src.core.cache.memory_cache import InMemoryCache
from src.core.cache.serializer import JsonSerializer
from src.user.schemas import UserProfile

SEEN_AT = datetime(2026, 1, 1, 12, tzinfo=UTC)


def build_user_snapshot(**overrides: object) -> UserProfile:
    """User 1 as the use case tests' TelegramProfile describes them."""
    snapshot = UserProfile(
        id=1,
        first_name="Ann",
        username="ann",
        last_name=None,
        language_code="en",
        locale=None,
        is_reachable=True,
        last_seen_at=SEEN_AT,
    )
    return replace(snapshot, **overrides)


def build_cache() -> InMemoryCache:
    return InMemoryCache(JsonSerializer(), default_ttl=60, version_ttl=600)
