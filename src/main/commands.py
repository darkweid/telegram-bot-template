from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats
from aiogram.utils.i18n import I18n

from src.core.i18n import SUPPORTED_LOCALES
from src.user.commands import USER_COMMANDS


async def set_bot_commands(bot: Bot, i18n: I18n) -> None:
    """Publish the command menu: once per supported language, and once
    without a language for users whose language we do not support."""
    scope = BotCommandScopeAllPrivateChats()
    for language_code in (None, *SUPPORTED_LOCALES):
        locale = language_code or i18n.default_locale
        commands = [
            BotCommand(
                command=spec.command,
                description=i18n.gettext(spec.description, locale=locale),
            )
            for spec in USER_COMMANDS
        ]
        await bot.set_my_commands(commands, scope=scope, language_code=language_code)
