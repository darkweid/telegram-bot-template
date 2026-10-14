from aiogram import F, Router
from aiogram.enums import ChatType

from src.core.telegram.roles import IsAdmin


def build_admin_router() -> Router:
    """Admin commands, private chats only. A non-admin's command falls
    through to the routers below, so the bot does not reveal it exists."""
    router = Router(name="admin")
    router.message.filter(F.chat.type == ChatType.PRIVATE, IsAdmin())
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE, IsAdmin())
    return router
