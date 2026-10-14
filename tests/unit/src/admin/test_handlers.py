from unittest.mock import AsyncMock

from aiogram.methods import SendMessage
from aiogram.utils.i18n import I18n
from dishka import Provider, Scope, provide

from src.main.config import Config
from src.user.exceptions import ProtectedUserError
from src.user.schemas import BanChange, UserStats
from src.user.usecases.change_ban import ChangeBanUseCase
from src.user.usecases.get_user_stats import GetUserStatsUseCase
from tests.factories.updates import build_message_update
from tests.factories.users import build_user_snapshot
from tests.helpers.dispatcher import running_bot
from tests.helpers.user import UseCases

ADMIN = 42


class BanStub(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.change = AsyncMock(spec=ChangeBanUseCase)
        self.change.execute.return_value = BanChange(
            user=build_user_snapshot(id=5, first_name="<Bob>", is_banned=True),
            changed=True,
        )

    @provide(scope=Scope.REQUEST, override=True)
    def change_ban(self) -> ChangeBanUseCase:
        return self.change


def _admin(settings: Config) -> Config:
    return settings.model_copy(
        update={
            "bot": settings.bot.model_copy(update={"ADMIN_IDS": frozenset({ADMIN})})
        }
    )


async def _reply(
    settings: Config, i18n: I18n, text: str, stub: BanStub, user_id: int = ADMIN
) -> list[str]:
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update(text, user_id=user_id))
        return [reply.text or "" for reply in running.session.sent(SendMessage)]


async def test_ban_by_id_confirms_with_the_escaped_name(
    settings: Config, i18n: I18n
) -> None:
    stub = BanStub()
    [reply] = await _reply(settings, i18n, "/ban 5", stub)
    stub.change.execute.assert_awaited_once_with(ADMIN, 5, banned=True)
    assert "&lt;Bob&gt;" in reply and "5" in reply


async def test_unban_by_username(settings: Config, i18n: I18n) -> None:
    stub = BanStub()
    stub.change.execute.return_value = BanChange(
        user=build_user_snapshot(id=5, username="bob_smith"), changed=True
    )
    [reply] = await _reply(settings, i18n, "/unban @bob_smith", stub)
    stub.change.execute.assert_awaited_once_with(ADMIN, "bob_smith", banned=False)
    assert reply == "Ann (5) is unbanned."


async def test_unbanning_a_user_who_is_not_banned_says_so(
    settings: Config, i18n: I18n
) -> None:
    stub = BanStub()
    stub.change.execute.return_value = BanChange(
        user=build_user_snapshot(id=5), changed=False
    )
    [reply] = await _reply(settings, i18n, "/unban 5", stub)
    assert reply == "Ann (5) is not banned."


async def test_ban_without_a_user_shows_the_usage(settings: Config, i18n: I18n) -> None:
    stub = BanStub()
    [reply] = await _reply(settings, i18n, "/ban", stub)
    assert "/ban" in reply
    stub.change.execute.assert_not_awaited()


async def test_a_refusal_is_explained(settings: Config, i18n: I18n) -> None:
    stub = BanStub()
    stub.change.execute.side_effect = ProtectedUserError()
    [reply] = await _reply(settings, i18n, "/ban 2", stub)
    assert reply == "Admins and developers cannot be banned."


async def test_a_repeated_ban_says_so(settings: Config, i18n: I18n) -> None:
    stub = BanStub()
    stub.change.execute.return_value = BanChange(
        user=build_user_snapshot(id=5, is_banned=True), changed=False
    )
    [reply] = await _reply(settings, i18n, "/ban 5", stub)
    assert reply == "Ann (5) is already banned."


async def test_a_non_admin_cannot_ban(settings: Config, i18n: I18n) -> None:
    """The command must not even be acknowledged as existing."""
    stub = BanStub()
    await _reply(settings, i18n, "/ban 5", stub, user_id=77)
    stub.change.execute.assert_not_awaited()


class StatsStub(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.stats = AsyncMock(spec=GetUserStatsUseCase)
        self.stats.execute.return_value = UserStats(
            total=1,
            reachable=1,
            banned=0,
            active_day=1,
            active_week=1,
            active_month=1,
            new_day=0,
            new_week=1,
            new_month=1,
        )

    @provide(scope=Scope.REQUEST, override=True)
    def get_user_stats(self) -> GetUserStatsUseCase:
        return self.stats


async def test_stats_are_shown_to_an_admin(settings: Config, i18n: I18n) -> None:
    """The singular form matters: "1 users" is the first thing an admin of a
    new bot would see."""
    stub = StatsStub()
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/stats", user_id=ADMIN))
        [reply] = running.session.sent(SendMessage)
    assert reply.text == (
        "1 user\n"
        "Reachable: 1\n"
        "Banned: 0\n"
        "Active in the last day / week / month: 1 / 1 / 1\n"
        "New in the last day / week / month: 0 / 1 / 1"
    )


async def test_stats_are_hidden_from_everyone_else(
    settings: Config, i18n: I18n
) -> None:
    """User counts are business data; a non-admin must not learn the command
    exists."""
    stub = StatsStub()
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/stats", user_id=77))
    stub.stats.execute.assert_not_awaited()
