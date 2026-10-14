from aiogram import F, Router
from aiogram.enums import ChatType

from src.core.telegram.roles import IsDeveloper


def build_developer_router() -> Router:
    """Developer commands, private chats only. A non-developer's command
    falls through to the routers below, so the bot does not reveal it exists."""
    router = Router(name="developer")
    router.message.filter(F.chat.type == ChatType.PRIVATE, IsDeveloper())
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE, IsDeveloper())
    return router
