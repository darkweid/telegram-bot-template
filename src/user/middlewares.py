from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Chat, TelegramObject, Update, User
from dishka import AsyncContainer
from dishka.integrations.aiogram import CONTAINER_NAME

from src.user.schemas import TelegramProfile
from src.user.usecases.touch_user import TouchUserUseCase


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
