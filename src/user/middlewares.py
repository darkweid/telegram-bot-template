from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Chat, TelegramObject, Update, User
from dishka import AsyncContainer
from dishka.integrations.aiogram import CONTAINER_NAME

from loggers import get_logger
from src.core.telegram.roles import Roles
from src.user.schemas import TelegramProfile, UserProfile
from src.user.usecases.touch_user import TouchUserUseCase

logger = get_logger(__name__)


class UserMiddleware(BaseMiddleware):
    """Store the sender of every private-chat update and hand handlers the
    stored user as `user`.

    Skipped, so `user` is absent:
    - updates with no sender, and senders that are bots;
    - group and channel updates: the users table holds people who talk to
      the bot, not every member of every group it is in;
    - `my_chat_member`: its own handler records the membership change, and
      touching the user first would undo the unreachable mark it reports.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        sender: User | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")
        if (
            sender is None
            or sender.is_bot
            or chat is None
            or chat.type != ChatType.PRIVATE
            or (isinstance(event, Update) and event.my_chat_member is not None)
        ):
            return await handler(event, data)
        container: AsyncContainer = data[CONTAINER_NAME]
        touch = await container.get(TouchUserUseCase)
        data["user"] = await touch.execute(TelegramProfile.from_aiogram(sender))
        return await handler(event, data)


class BanMiddleware(BaseMiddleware):
    """Drop every update of a banned user, without an answer.

    Reads the snapshot UserMiddleware stored; an update without one (a group,
    `my_chat_member`) passes, so a banned user's reachability is still
    tracked. A role beats a ban here, for updates only: someone banned before
    joining ADMIN_IDS or DEVELOPER_IDS is not locked out, but broadcasts skip
    them and /stats counts them as banned until an admin unbans them.

    Only updates that carry a stored user (private chats) are checked; a router
    for inline queries or groups must check `is_banned` itself.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: UserProfile | None = data.get("user")
        roles: Roles = data["roles"]
        if user is not None and user.is_banned and not roles.is_privileged(user.id):
            logger.info("Dropped an update of banned user %s", user.id)
            return None
        return await handler(event, data)
