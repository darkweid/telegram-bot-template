from aiogram import F, Router, html
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message
from dishka import FromDishka

from src.core.telegram.alerts import DeveloperAlerter
from src.core.telegram.roles import IsDeveloper
from src.developer.status import render_status
from src.main.config import AppConfig
from src.system.services import HealthService


async def status(
    message: Message,
    health: FromDishka[HealthService],
    app_config: FromDishka[AppConfig],
) -> None:
    report = await health.get_report()
    await message.answer(render_status(report, app_config.PROJECT_NAME))


async def send_test_alert(
    message: Message,
    alerter: FromDishka[DeveloperAlerter],
    app_config: FromDishka[AppConfig],
) -> None:
    sender = message.from_user.id if message.from_user else "unknown"
    await alerter.send(
        f"Test alert from <b>{html.quote(app_config.PROJECT_NAME)}</b> "
        f"{html.quote(app_config.VERSION)} ({html.quote(app_config.COMMIT_SHA)}), "
        f"requested by {sender}"
    )
    await message.answer("Test alert sent. Not there? The bot log says why.")


def build_developer_router() -> Router:
    """Developer commands, private chats only. A non-developer's command
    falls through to the routers below, so the bot does not reveal it exists."""
    router = Router(name="developer")
    router.message.filter(F.chat.type == ChatType.PRIVATE, IsDeveloper())
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE, IsDeveloper())
    router.message.register(status, Command("status"))
    router.message.register(send_test_alert, Command("test_alert"))
    return router
