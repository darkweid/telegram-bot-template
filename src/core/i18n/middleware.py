from typing import Any, Protocol

from aiogram.types import TelegramObject, User
from aiogram.utils.i18n import I18nMiddleware

from src.core.i18n import resolve_locale


class LocalePreference(Protocol):
    """What the stored user offers this middleware: the language they chose."""

    @property
    def locale(self) -> str | None: ...


class UserLocaleMiddleware(I18nMiddleware):
    """Pick the language of every reply.

    The user's choice comes first, then Telegram's language for them, then the
    bot's default.

    Registered after the user middleware, which puts the stored user into
    `data["user"]`; an update without one (a group chat, a channel post)
    falls back to Telegram's language.
    """

    async def get_locale(self, event: TelegramObject, data: dict[str, Any]) -> str:
        stored: LocalePreference | None = data.get("user")
        sender: User | None = data.get("event_from_user")
        return resolve_locale(
            stored.locale if stored else None,
            sender.language_code if sender else None,
            default=self.i18n.default_locale,
        )
