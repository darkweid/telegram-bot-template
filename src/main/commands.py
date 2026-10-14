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
from src.user.commands import USER_COMMANDS

logger = get_logger(__name__)


async def set_bot_commands(bot: Bot, i18n: I18n, roles: Roles) -> None:
    """Publish the command menu: once per supported language, and once
    without a language for users whose language we do not support. Every
    admin and developer then gets a menu of their own in their private chat."""
    await _set_menu(bot, i18n, USER_COMMANDS, BotCommandScopeAllPrivateChats())
    for chat_id in roles.members:
        specs = list(USER_COMMANDS)
        if roles.is_admin(chat_id):
            specs += ADMIN_COMMANDS
        if roles.is_developer(chat_id):
            specs += DEVELOPER_COMMANDS
        try:
            await _set_menu(bot, i18n, specs, BotCommandScopeChat(chat_id=chat_id))
        except (TelegramBadRequest, TelegramForbiddenError):
            # "chat not found" until the member opens the bot, Forbidden once
            # they block it; either way the rest of the menus still go out.
            logger.warning("Cannot set the role menu for chat %s", chat_id)


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
