from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware, Dispatcher
from aiogram.types import Message, TelegramObject
from aiogram.utils.i18n import I18n, gettext as _
import pytest

from src.core.i18n.middleware import UserLocaleMiddleware
from tests.factories.updates import build_message_update
from tests.fakes.telegram import FakeTelegramSession, make_bot


@dataclass(frozen=True)
class StoredUser:
    locale: str | None


class PutUser(BaseMiddleware):
    def __init__(self, user: StoredUser | None) -> None:
        self._user = user

    async def __call__(
        self, handler: Any, event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        if self._user is not None:
            data["user"] = self._user
        return await handler(event, data)


@pytest.mark.parametrize(
    ("stored", "language_code", "expected"),
    [
        (StoredUser(locale="ru"), "en", "ru"),  # the user's choice wins
        (StoredUser(locale=None), "ru-RU", "ru"),  # then Telegram's language
        (None, "ru", "ru"),  # a user we do not store (group chat)
        (StoredUser(locale=None), "de", "en"),  # unsupported -> default
        (StoredUser(locale=None), None, "en"),  # no language at all -> default
        (StoredUser(locale="xx"), "ru", "ru"),  # a stale stored locale is skipped
    ],
)
async def test_locale_resolution_order(
    i18n: I18n, stored: StoredUser | None, language_code: str | None, expected: str
) -> None:
    seen: list[str] = []
    dp = Dispatcher()
    dp.update.outer_middleware(PutUser(stored))
    dp.update.outer_middleware(UserLocaleMiddleware(i18n))

    @dp.message()
    async def handler(message: Message) -> None:
        seen.append(i18n.current_locale)
        _("Something went wrong. Please try again later.")  # gettext works inside

    await dp.feed_update(
        make_bot(FakeTelegramSession()),
        build_message_update(language_code=language_code),
    )
    assert seen == [expected]
