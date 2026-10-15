from decimal import Decimal
from unittest.mock import AsyncMock

from aiogram.methods import SendMessage
from aiogram.utils.i18n import I18n
from dishka import Provider, Scope, provide
import pytest

from src.admin.schemas import LLMUsageStats
from src.admin.usecases.get_llm_usage_stats import GetLLMUsageStatsUseCase
from src.core.llm.repository import (
    LLMUsageByPurpose,
    LLMUsageSummary,
    LLMUsageTopUser,
)
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
    replies = await _reply(settings, i18n, "/ban 5", stub, user_id=77)
    stub.change.execute.assert_not_awaited()
    assert replies == []


def _summary(
    calls: int, cost: str, *, errors: int = 0, unpriced: int = 0
) -> LLMUsageSummary:
    return LLMUsageSummary(
        calls=calls,
        errors=errors,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal(cost),
        unpriced_calls=unpriced,
    )


EMPTY_LLM = LLMUsageStats(
    today=_summary(0, "0"),
    week=_summary(0, "0"),
    month=_summary(0, "0"),
    by_purpose=[],
    top_users=[],
)


class StatsStub(Provider):
    # `total` stays first and positional: the plural test passes it.
    def __init__(self, total: int = 1, llm_usage: LLMUsageStats = EMPTY_LLM) -> None:
        super().__init__()
        self.stats = AsyncMock(spec=GetUserStatsUseCase)
        self.stats.execute.return_value = UserStats(
            total=total,
            reachable=1,
            banned=0,
            active_day=1,
            active_week=1,
            active_month=1,
            new_day=0,
            new_week=1,
            new_month=1,
        )
        self.llm_usage = AsyncMock(spec=GetLLMUsageStatsUseCase)
        self.llm_usage.execute.return_value = llm_usage

    @provide(scope=Scope.REQUEST, override=True)
    def get_user_stats(self) -> GetUserStatsUseCase:
        return self.stats

    @provide(scope=Scope.REQUEST, override=True)
    def get_llm_usage_stats(self) -> GetLLMUsageStatsUseCase:
        return self.llm_usage


USER_BLOCK = (
    "1 user\n"
    "Reachable: 1\n"
    "Banned: 0\n"
    "Active in the last day / week / month: 1 / 1 / 1\n"
    "New in the last day / week / month: 0 / 1 / 1"
)


async def _stats_reply(settings: Config, i18n: I18n, stub: StatsStub) -> str:
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/stats", user_id=ADMIN))
        [reply] = running.session.sent(SendMessage)
    return reply.text or ""


async def test_stats_are_shown_to_an_admin(settings: Config, i18n: I18n) -> None:
    """The singular forms matter: "1 users" or "1 calls" is the first thing an
    admin of a new bot would see."""
    reply = await _stats_reply(settings, i18n, StatsStub())
    assert reply == (
        f"{USER_BLOCK}\n\n"
        "LLM spending (days are UTC):\n"
        "Today: $0.0000, 0 calls\n"
        "Last 7 days: $0.0000, 0 calls\n"
        "Last 30 days: $0.0000, 0 calls"
    )


async def test_llm_spending_shows_purposes_top_users_and_gaps(
    settings: Config, i18n: I18n
) -> None:
    """Failed and unpriced calls mean the total is not the whole bill; the
    admin must see that next to the number."""
    month = _summary(12, "0.0421", errors=2, unpriced=1)
    stub = StatsStub(
        llm_usage=LLMUsageStats(
            today=_summary(1, "0.0031"),
            week=month,
            month=month,
            by_purpose=[
                LLMUsageByPurpose(
                    purpose="note_summary", calls=12, cost_usd=Decimal("0.0421")
                )
            ],
            top_users=[
                LLMUsageTopUser(user_id=5, calls=11, cost_usd=Decimal("0.04")),
                LLMUsageTopUser(user_id=7, calls=1, cost_usd=Decimal("0.0021")),
            ],
        )
    )
    reply = await _stats_reply(settings, i18n, stub)
    assert reply.split("\n\n")[1] == (
        "LLM spending (days are UTC):\n"
        "Today: $0.0031, 1 call\n"
        "Last 7 days: $0.0421, 12 calls\n"
        "Last 30 days: $0.0421, 12 calls\n"
        "Errors in the last 30 days: 2\n"
        "Without a known price in the last 30 days: 1\n"
        "By purpose, last 30 days:\n"
        "note_summary: $0.0421, 12 calls\n"
        "Top users, last 30 days:\n"
        "5: $0.0400, 11 calls\n"
        "7: $0.0021, 1 call"
    )


async def test_stats_are_hidden_from_everyone_else(
    settings: Config, i18n: I18n
) -> None:
    """User counts and model spending are business data; a non-admin must not
    learn the command exists."""
    stub = StatsStub()
    async with running_bot(_admin(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/stats", user_id=77))
        replies = running.session.sent(SendMessage)
    stub.stats.execute.assert_not_awaited()
    stub.llm_usage.execute.assert_not_awaited()
    assert replies == []


@pytest.mark.parametrize(
    ("total", "first_line"),
    [(1, "1 пользователь"), (2, "2 пользователя"), (5, "5 пользователей")],
)
async def test_stats_are_translated_with_all_three_plural_forms(
    settings: Config, i18n: I18n, total: int, first_line: str
) -> None:
    """Russian has three plural forms; a broken msgstr shows up only here, at
    the handler, not in the catalog structure checks."""
    stub = StatsStub(total)
    async with running_bot(
        _admin(settings), i18n, UseCases(locale="ru"), stub
    ) as running:
        await running.feed(build_message_update("/stats", user_id=ADMIN))
        [reply] = running.session.sent(SendMessage)
    assert reply.text is not None
    assert reply.text.splitlines()[0] == first_line
    assert "Забанены: 0" in reply.text
