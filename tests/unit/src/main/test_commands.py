from aiogram.methods import SetMyCommands
from aiogram.types import BotCommandScopeAllPrivateChats
from aiogram.utils.i18n import I18n

from src.core.i18n import SUPPORTED_LOCALES
from src.main.commands import set_bot_commands
from tests.fakes.telegram import FakeTelegramSession, make_bot


async def test_menu_is_set_for_every_language_and_the_default(i18n: I18n) -> None:
    session = FakeTelegramSession()
    await set_bot_commands(make_bot(session), i18n)
    calls = session.sent(SetMyCommands)
    assert [call.language_code for call in calls] == [None, *SUPPORTED_LOCALES]
    assert all(isinstance(call.scope, BotCommandScopeAllPrivateChats) for call in calls)
    russian = next(call for call in calls if call.language_code == "ru")
    assert [(c.command, c.description) for c in russian.commands] == [
        ("start", "Запустить бота"),
        ("help", "Показать справку"),
        ("language", "Сменить язык"),
    ]
    default = next(call for call in calls if call.language_code is None)
    expected = next(call for call in calls if call.language_code == i18n.default_locale)
    assert [(c.command, c.description) for c in default.commands] == [
        (c.command, c.description) for c in expected.commands
    ]
