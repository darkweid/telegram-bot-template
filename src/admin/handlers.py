from decimal import Decimal

from aiogram import F, Router, html
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _, ngettext
from dishka import FromDishka

from src.admin.schemas import LLMUsageStats
from src.admin.usecases.get_llm_usage_stats import GetLLMUsageStatsUseCase
from src.core.telegram.roles import IsAdmin
from src.user.schemas import UserStats
from src.user.services import parse_user_reference
from src.user.usecases.change_ban import ChangeBanUseCase
from src.user.usecases.get_user_stats import GetUserStatsUseCase


def build_admin_router() -> Router:
    """Admin commands, private chats only. A non-admin's command falls
    through to the routers below, so the bot does not reveal it exists."""
    router = Router(name="admin")
    router.message.filter(F.chat.type == ChatType.PRIVATE, IsAdmin())
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE, IsAdmin())
    router.message.register(ban, Command("ban"))
    router.message.register(unban, Command("unban"))
    router.message.register(stats, Command("stats"))
    return router


async def ban(
    message: Message,
    command: CommandObject,
    change_ban: FromDishka[ChangeBanUseCase],
) -> None:
    await _change_ban(message, command, change_ban, banned=True)


async def unban(
    message: Message,
    command: CommandObject,
    change_ban: FromDishka[ChangeBanUseCase],
) -> None:
    await _change_ban(message, command, change_ban, banned=False)


async def _change_ban(
    message: Message,
    command: CommandObject,
    change_ban: ChangeBanUseCase,
    *,
    banned: bool,
) -> None:
    reference = parse_user_reference(command.args)
    if reference is None or message.from_user is None:
        await message.answer(
            _("Usage: /{command} USER_ID or /{command} @username").format(
                command=command.command
            )
        )
        return
    change = await change_ban.execute(message.from_user.id, reference, banned=banned)
    name = html.quote(change.user.first_name)
    if not change.changed:
        text = (
            _("{name} ({id}) is already banned.")
            if banned
            else _("{name} ({id}) is not banned.")
        )
    else:
        text = (
            _("{name} ({id}) is banned.") if banned else _("{name} ({id}) is unbanned.")
        )
    await message.answer(text.format(name=name, id=change.user.id))


async def stats(
    message: Message,
    get_user_stats: FromDishka[GetUserStatsUseCase],
    get_llm_usage_stats: FromDishka[GetLLMUsageStatsUseCase],
) -> None:
    counts = await get_user_stats.execute()
    usage = await get_llm_usage_stats.execute()
    await message.answer(
        "\n\n".join((_user_stats_text(counts), _llm_usage_text(usage)))
    )


def _user_stats_text(counts: UserStats) -> str:
    return "\n".join(
        (
            ngettext("{count} user", "{count} users", counts.total).format(
                count=counts.total
            ),
            _("Reachable: {count}").format(count=counts.reachable),
            _("Banned: {count}").format(count=counts.banned),
            _("Active in the last day / week / month: {day} / {week} / {month}").format(
                day=counts.active_day,
                week=counts.active_week,
                month=counts.active_month,
            ),
            _("New in the last day / week / month: {day} / {week} / {month}").format(
                day=counts.new_day, week=counts.new_week, month=counts.new_month
            ),
        )
    )


def _llm_usage_text(usage: LLMUsageStats) -> str:
    lines = [
        _("LLM spending (days are UTC):"),
        ngettext(
            "Today: ${cost}, {count} call",
            "Today: ${cost}, {count} calls",
            usage.today.calls,
        ).format(cost=_usd(usage.today.cost_usd), count=usage.today.calls),
        ngettext(
            "Last 7 days: ${cost}, {count} call",
            "Last 7 days: ${cost}, {count} calls",
            usage.week.calls,
        ).format(cost=_usd(usage.week.cost_usd), count=usage.week.calls),
        ngettext(
            "Last 30 days: ${cost}, {count} call",
            "Last 30 days: ${cost}, {count} calls",
            usage.month.calls,
        ).format(cost=_usd(usage.month.cost_usd), count=usage.month.calls),
    ]
    if usage.month.errors:
        lines.append(
            _("Errors in the last 30 days: {count}").format(count=usage.month.errors)
        )
    if usage.month.unpriced_calls:
        # Their cost is unknown, so the totals above are short by that much.
        lines.append(
            _("Without a known price in the last 30 days: {count}").format(
                count=usage.month.unpriced_calls
            )
        )
    if usage.by_purpose:
        lines.append(_("By purpose, last 30 days:"))
        lines += [
            _spending_line(row.purpose, row.cost_usd, row.calls)
            for row in usage.by_purpose
        ]
    if usage.top_users:
        lines.append(_("Top users, last 30 days:"))
        lines += [
            _spending_line(str(row.user_id), row.cost_usd, row.calls)
            for row in usage.top_users
        ]
    return "\n".join(lines)


def _spending_line(name: str, cost: Decimal, calls: int) -> str:
    return ngettext(
        "{name}: ${cost}, {count} call", "{name}: ${cost}, {count} calls", calls
    ).format(name=name, cost=_usd(cost), count=calls)


def _usd(value: Decimal) -> str:
    # One summary costs a fraction of a cent; two decimals would show $0.00.
    return f"{value:.4f}"
