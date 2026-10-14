from aiogram.methods import SendMessage
from aiogram.utils.i18n import I18n

from src.main.config import Config
from tests.factories.updates import build_message_update
from tests.helpers.dispatcher import running_bot
from tests.helpers.user import UseCases


def _with_admin(settings: Config, admin: int) -> Config:
    return settings.model_copy(
        update={
            "bot": settings.bot.model_copy(update={"ADMIN_IDS": frozenset({admin})})
        }
    )


async def test_a_banned_users_updates_are_dropped_silently(
    settings: Config, i18n: I18n
) -> None:
    """An answer would tell them the bot is alive and worth pestering."""
    async with running_bot(settings, i18n, UseCases(banned=True)) as running:
        await running.feed(build_message_update("/start"))
        assert running.session.sent(SendMessage) == []


async def test_a_privileged_user_with_a_stale_ban_is_not_dropped(
    settings: Config, i18n: I18n
) -> None:
    """Someone banned before they joined ADMIN_IDS would otherwise be locked
    out of /unban itself."""
    async with running_bot(
        _with_admin(settings, 42), i18n, UseCases(banned=True)
    ) as running:
        await running.feed(build_message_update("/start", user_id=42))
        assert len(running.session.sent(SendMessage)) == 1


async def test_a_user_who_is_not_banned_is_answered(
    settings: Config, i18n: I18n
) -> None:
    async with running_bot(settings, i18n, UseCases()) as running:
        await running.feed(build_message_update("/start"))
        assert len(running.session.sent(SendMessage)) == 1
