from datetime import UTC, datetime, timedelta

import pytest

from src.user.schemas import TelegramProfile, UserProfile
from src.user.services import UserReference, needs_write, parse_user_reference

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
INTERVAL = timedelta(minutes=5)
PROFILE = TelegramProfile(
    id=1, first_name="Ann", username="ann", last_name=None, language_code="en"
)


def _stored(**overrides: object) -> UserProfile:
    fields: dict[str, object] = {
        "id": 1,
        "first_name": "Ann",
        "username": "ann",
        "last_name": None,
        "language_code": "en",
        "locale": None,
        "is_reachable": True,
        "is_banned": False,
        "last_seen_at": NOW - timedelta(minutes=1),
    }
    return UserProfile(**(fields | overrides))


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, True),  # new user
        (_stored(), False),  # seen a minute ago, nothing changed
        (_stored(username="old"), True),  # profile changed
        (_stored(is_reachable=False), True),  # came back after being unreachable
        (_stored(last_seen_at=NOW - INTERVAL), True),  # interval reached
        (_stored(last_seen_at=NOW - INTERVAL + timedelta(seconds=1)), False),
    ],
)
def test_needs_write(stored: UserProfile | None, expected: bool) -> None:
    assert needs_write(stored, PROFILE, NOW, INTERVAL) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("123456789", 123456789),
        ("  42 ", 42),
        ("@Ann_Smith", "Ann_Smith"),
        ("Ann_Smith", "Ann_Smith"),
    ],
)
def test_a_user_is_named_by_id_or_username(text: str, expected: UserReference) -> None:
    assert parse_user_reference(text) == expected


@pytest.mark.parametrize(
    "text", [None, "", "   ", "@", "0", "-5", "@ab", "two words", "@bad-name"]
)
def test_anything_else_is_not_a_user(text: str | None) -> None:
    """Telegram usernames are 5 to 32 letters, digits and underscores; user
    ids are positive. Anything else is a typo, answered with the usage line."""
    assert parse_user_reference(text) is None
