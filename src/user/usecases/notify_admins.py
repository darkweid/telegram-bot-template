from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.i18n import I18n

from loggers import get_logger
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.errors.exceptions import format_html
from src.core.i18n import N_, resolve_locale
from src.core.telegram.roles import Roles
from src.main.config import BotConfig
from src.user.repositories import UserRepository

logger = get_logger(__name__)

NEW_USER_WITH_USERNAME = N_("New user: {name} (@{username}), id {id}")
NEW_USER = N_("New user: {name}, id {id}")


@dataclass(frozen=True, slots=True)
class _Recipient:
    chat_id: int
    locale: str


class NotifyAdminsOfNewUserUseCase:
    """Tell every admin who has talked to the bot that a new user arrived.

    Each admin hears it in their own language, chosen like every reply: their
    /language choice, then their Telegram language, then DEFAULT_LOCALE. An
    admin with no stored row is skipped: Telegram refuses a bot's first
    message. A failed delivery to one admin is logged and the rest still get
    theirs; the task is not retried, so nobody gets the notice twice.
    """

    def __init__(
        self,
        uow: SQLAlchemyUnitOfWork,
        users: UserRepository,
        bot: Bot,
        i18n: I18n,
        roles: Roles,
        bot_config: BotConfig,
    ) -> None:
        self._uow = uow
        self._users = users
        self._bot = bot
        self._i18n = i18n
        self._roles = roles
        self._enabled = bot_config.NOTIFY_ADMINS_ON_NEW_USER
        self._default_locale = bot_config.DEFAULT_LOCALE

    async def execute(self, user_id: int) -> int:
        """Send the notice; returns how many admins received it."""
        if not self._enabled:
            return 0
        # Values are copied out inside the block: leaving it without a commit
        # rolls back and expires the ORM objects.
        async with self._uow as uow:
            user = await self._users.get_single(uow.session, id=user_id)
            if user is None:
                logger.warning("New user %s is gone; no admin notice", user_id)
                return 0
            params = {"name": user.first_name, "username": user.username, "id": user_id}
            recipients: list[_Recipient] = []
            for admin_id in sorted(self._roles.admins - {user_id}):
                admin = await self._users.get_single(uow.session, id=admin_id)
                if admin is None:
                    continue
                locale = resolve_locale(
                    admin.locale, admin.language_code, default=self._default_locale
                )
                recipients.append(_Recipient(admin_id, locale))
        # The reads are done: the Bot API calls below hold no connection.
        template = NEW_USER_WITH_USERNAME if params["username"] else NEW_USER
        sent = 0
        for recipient in recipients:
            # Locale passed explicitly: a task runs outside any update, so no
            # middleware made an I18n current for the `_` shortcut.
            text = format_html(
                self._i18n.gettext(template, locale=recipient.locale), params
            )
            try:
                await self._bot.send_message(recipient.chat_id, text)
            except TelegramAPIError as error:
                logger.warning(
                    "New user notice to admin %s failed: %s",
                    recipient.chat_id,
                    type(error).__name__,
                )
                continue
            sent += 1
        return sent
