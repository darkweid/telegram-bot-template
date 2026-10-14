from datetime import UTC, datetime

from aiogram.types import User as TelegramUser

from src.user.models import User
from src.user.schemas import TelegramProfile, UserProfile

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _telegram_user(**overrides: object) -> TelegramUser:
    fields: dict[str, object] = {
        "id": 42,
        "is_bot": False,
        "first_name": "Ann",
        "last_name": "Lee",
        "username": "ann",
        "language_code": "ru",
    }
    return TelegramUser(**(fields | overrides))


def _stored(**overrides: object) -> UserProfile:
    fields: dict[str, object] = {
        "id": 42,
        "first_name": "Ann",
        "username": "ann",
        "last_name": "Lee",
        "language_code": "ru",
        "locale": None,
        "is_reachable": True,
        "is_banned": False,
        "last_seen_at": NOW,
    }
    return UserProfile(**(fields | overrides))


def test_profile_takes_the_fields_we_store() -> None:
    assert TelegramProfile.from_aiogram(_telegram_user()) == TelegramProfile(
        id=42, first_name="Ann", username="ann", last_name="Lee", language_code="ru"
    )


def test_profile_keeps_missing_optional_fields_as_none() -> None:
    profile = TelegramProfile.from_aiogram(
        _telegram_user(last_name=None, username=None, language_code=None)
    )
    assert (profile.username, profile.last_name, profile.language_code) == (
        None,
        None,
        None,
    )


def test_user_profile_is_built_from_the_model() -> None:
    model = User(
        id=42,
        first_name="Ann",
        username="ann",
        last_name="Lee",
        language_code="ru",
        locale="en",
        is_reachable=True,
        is_banned=False,
        last_seen_at=NOW,
    )
    assert UserProfile.from_model(model) == _stored(locale="en")


def test_same_profile_does_not_differ() -> None:
    assert not _stored().differs_from(TelegramProfile.from_aiogram(_telegram_user()))


def test_any_changed_field_differs() -> None:
    for change in (
        {"username": "ann2"},
        {"first_name": "Anna"},
        {"last_name": None},
        {"language_code": "en"},
    ):
        assert _stored().differs_from(
            TelegramProfile.from_aiogram(_telegram_user(**change))
        ), change
