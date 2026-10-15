from collections.abc import Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeUnion,
)
from aiogram.utils.i18n import I18n

from loggers import get_logger
from src.admin.commands import ADMIN_COMMANDS
from src.core.i18n import SUPPORTED_LOCALES
from src.core.telegram.commands import BotCommandSpec
from src.core.telegram.roles import Roles
from src.developer.commands import DEVELOPER_COMMANDS
from src.note.commands import NOTE_COMMANDS
from src.user.commands import USER_COMMANDS

logger = get_logger(__name__)

# The menu of every private chat and the list /help prints, in this order.
# A new domain with commands adds its tuple here.
PUBLIC_COMMANDS: tuple[BotCommandSpec, ...] = (*USER_COMMANDS, *NOTE_COMMANDS)


async def set_bot_commands(bot: Bot, i18n: I18n, roles: Roles) -> None:
    """Publish the command menu (PUBLIC_COMMANDS): once per supported
    language, and once without a language for users whose language we do not
    support. Every admin and developer then gets a menu of their own in their
    private chat."""
    await _set_menu(bot, i18n, PUBLIC_COMMANDS, BotCommandScopeAllPrivateChats())
    for chat_id in roles.members:
        specs = list(PUBLIC_COMMANDS)
        if roles.is_admin(chat_id):
            specs += ADMIN_COMMANDS
        if roles.is_developer(chat_id):
            specs += DEVELOPER_COMMANDS
        try:
            await _set_menu(bot, i18n, specs, BotCommandScopeChat(chat_id=chat_id))
        except TelegramForbiddenError as error:
            # The member blocked the bot.
            _warn_menu_skipped(chat_id, error)
        except TelegramBadRequest as error:
            # Only "chat not found" (the member never opened the bot) is
            # tolerated; any other rejection is a bug in a command spec and
            # must fail startup instead of silently dropping every role menu.
            if "chat not found" not in error.message.lower():
                raise
            _warn_menu_skipped(chat_id, error)


def _warn_menu_skipped(chat_id: int, error: Exception) -> None:
    logger.warning(
        "Cannot set the role menu for chat %s (%s)", chat_id, type(error).__name__
    )


async def _set_menu(
    bot: Bot,
    i18n: I18n,
    specs: Sequence[BotCommandSpec],
    scope: BotCommandScopeUnion,
) -> None:
    for language_code in (None, *SUPPORTED_LOCALES):
        locale = language_code or i18n.default_locale
        commands = [
            BotCommand(
                command=spec.command,
                description=i18n.gettext(spec.description, locale=locale),
            )
            for spec in specs
        ]
        await bot.set_my_commands(commands, scope=scope, language_code=language_code)
