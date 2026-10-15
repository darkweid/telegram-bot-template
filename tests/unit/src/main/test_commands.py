from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SetMyCommands
from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeChat
from aiogram.utils.i18n import I18n
import pytest

from src.core.i18n import SUPPORTED_LOCALES
from src.core.telegram.commands import BotCommandSpec
from src.core.telegram.roles import Roles
from src.main import commands as commands_module
from src.main.commands import PUBLIC_COMMANDS, set_bot_commands
from tests.fakes.telegram import FakeTelegramSession, make_bot
from tests.helpers.logs import captured_logs

NOBODY = Roles(admins=frozenset(), developers=frozenset())
ADMIN_ONLY = BotCommandSpec("admin_only", "Start the bot")
DEVELOPER_ONLY = BotCommandSpec("developer_only", "Show help")


@pytest.fixture
def role_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand-in role commands, so these tests do not change with every command
    a later feature adds. Their msgids reuse translated user strings."""
    monkeypatch.setattr(commands_module, "ADMIN_COMMANDS", (ADMIN_ONLY,))
    monkeypatch.setattr(commands_module, "DEVELOPER_COMMANDS", (DEVELOPER_ONLY,))


def _chat_calls(session: FakeTelegramSession, chat_id: int) -> list[SetMyCommands]:
    return [
        call
        for call in session.sent(SetMyCommands)
        if isinstance(call.scope, BotCommandScopeChat) and call.scope.chat_id == chat_id
    ]


async def test_menu_is_set_for_every_language_and_the_default(i18n: I18n) -> None:
    """Telegram shows the menu of the user's own language and falls back to the
    one without a language code; a missing entry leaves some users without one."""
    session = FakeTelegramSession()
    await set_bot_commands(make_bot(session), i18n, NOBODY)
    calls = session.sent(SetMyCommands)
    assert [call.language_code for call in calls] == [None, *SUPPORTED_LOCALES]
    assert all(isinstance(call.scope, BotCommandScopeAllPrivateChats) for call in calls)
    russian = next(call for call in calls if call.language_code == "ru")
    assert [(c.command, c.description) for c in russian.commands] == [
        ("start", "Запустить бота"),
        ("help", "Показать справку"),
        ("language", "Сменить язык"),
        ("note", "Написать заметку"),
        ("notes", "Мои заметки"),
        ("summarize", "Сводка последних заметок"),
    ]
    default = next(call for call in calls if call.language_code is None)
    expected = next(call for call in calls if call.language_code == i18n.default_locale)
    assert [(c.command, c.description) for c in default.commands] == [
        (c.command, c.description) for c in expected.commands
    ]


@pytest.mark.usefixtures("role_commands")
async def test_each_role_member_gets_the_public_menu_plus_their_role(
    i18n: I18n,
) -> None:
    """A chat scope replaces the private-chat menu for that chat, so it has to
    repeat PUBLIC_COMMANDS, or an admin would lose /start, /help and the
    note commands."""
    session = FakeTelegramSession()
    roles = Roles(admins=frozenset({10, 30}), developers=frozenset({20, 30}))
    await set_bot_commands(make_bot(session), i18n, roles)
    user_part = [spec.command for spec in PUBLIC_COMMANDS]
    for chat_id, extra in (
        (10, ["admin_only"]),
        (20, ["developer_only"]),
        (30, ["admin_only", "developer_only"]),
    ):
        calls = _chat_calls(session, chat_id)
        assert [call.language_code for call in calls] == [None, *SUPPORTED_LOCALES]
        assert all(
            [c.command for c in call.commands] == user_part + extra for call in calls
        )


@pytest.mark.usefixtures("role_commands")
async def test_role_menus_are_translated_per_language(i18n: I18n) -> None:
    """The chat-scope menu replaces the language-scoped one, so a Russian admin
    would see English commands if the role menu were set in one language."""
    session = FakeTelegramSession()
    roles = Roles(admins=frozenset({10}), developers=frozenset())
    await set_bot_commands(make_bot(session), i18n, roles)
    russian = next(c for c in _chat_calls(session, 10) if c.language_code == "ru")
    assert russian.commands[-1].description == "Запустить бота"


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SetMyCommands(commands=[]), message=message)


def _forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(
        method=SetMyCommands(commands=[]), message="bot was blocked by the user"
    )


def _queue_private_menus_then(session: FakeTelegramSession, error: Exception) -> None:
    """The private-chat menus go first, one call per language, all succeed;
    the next call (the first role menu, chat 10) fails with `error`."""
    for _ in (None, *SUPPORTED_LOCALES):
        session.queue(SetMyCommands, True)
    session.queue(SetMyCommands, error)


@pytest.mark.usefixtures("role_commands")
@pytest.mark.parametrize(
    ("error", "error_class"),
    [
        (_bad_request("Bad Request: chat not found"), "TelegramBadRequest"),
        (_forbidden(), "TelegramForbiddenError"),
    ],
)
async def test_an_unreachable_role_member_does_not_stop_startup(
    i18n: I18n, error: Exception, error_class: str
) -> None:
    """An id added to ADMIN_IDS before its owner opened the bot (chat not
    found), or whose owner blocked it, must not keep the bot from starting
    nor the other members from getting their menu."""
    session = FakeTelegramSession()
    roles = Roles(admins=frozenset({10, 20}), developers=frozenset())
    _queue_private_menus_then(session, error)
    with captured_logs("src.main.commands") as records:
        await set_bot_commands(make_bot(session), i18n, roles)
    assert len(_chat_calls(session, 10)) == 1
    assert len(_chat_calls(session, 20)) == 1 + len(SUPPORTED_LOCALES)
    assert [(r.levelname, r.args) for r in records] == [("WARNING", (10, error_class))]


@pytest.mark.usefixtures("role_commands")
async def test_a_rejected_command_spec_fails_startup(i18n: I18n) -> None:
    """An invalid command name or an over-long description is a bug in the
    code; swallowing it would leave every admin and developer without a menu
    and nobody the wiser."""
    session = FakeTelegramSession()
    roles = Roles(admins=frozenset({10}), developers=frozenset())
    _queue_private_menus_then(session, _bad_request("Bad Request: BOT_COMMAND_INVALID"))
    with pytest.raises(TelegramBadRequest):
        await set_bot_commands(make_bot(session), i18n, roles)
