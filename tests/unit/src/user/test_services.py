from datetime import UTC, datetime, timedelta

import pytest

from src.user.schemas import TelegramProfile, UserProfile
from src.user.services import needs_write

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
        "is_blocked": False,
        "last_seen_at": NOW - timedelta(minutes=1),
    }
    return UserProfile(**(fields | overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, True),  # new user
        (_stored(), False),  # seen a minute ago, nothing changed
        (_stored(username="old"), True),  # profile changed
        (_stored(is_blocked=True), True),  # came back after blocking
        (_stored(last_seen_at=NOW - INTERVAL), True),  # interval reached
        (_stored(last_seen_at=NOW - INTERVAL + timedelta(seconds=1)), False),
    ],
)
def test_needs_write(stored: UserProfile | None, expected: bool) -> None:
    assert needs_write(stored, PROFILE, NOW, INTERVAL) is expected
