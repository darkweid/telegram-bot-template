from aiogram import F, Router, html
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _, ngettext
from dishka import FromDishka

from src.core.telegram.roles import IsAdmin
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
    message: Message, get_user_stats: FromDishka[GetUserStatsUseCase]
) -> None:
    counts = await get_user_stats.execute()
    lines = (
        ngettext("{count} user", "{count} users", counts.total).format(
            count=counts.total
        ),
        _("Reachable: {count}").format(count=counts.reachable),
        _("Banned: {count}").format(count=counts.banned),
        _("Active in the last day / week / month: {day} / {week} / {month}").format(
            day=counts.active_day, week=counts.active_week, month=counts.active_month
        ),
        _("New in the last day / week / month: {day} / {week} / {month}").format(
            day=counts.new_day, week=counts.new_week, month=counts.new_month
        ),
    )
    await message.answer("\n".join(lines))
