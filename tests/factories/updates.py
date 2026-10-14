from datetime import UTC, datetime
from itertools import count
from typing import Literal

from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberBanned,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    Update,
    User,
)

from tests.fakes.telegram import bot_user

# Ids are unique within a process but depend on test order: tests must never
# assert a specific id.
_update_ids = count(1)
_message_ids = count(1)
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _resolve_update_id(update_id: int | None) -> int:
    return next(_update_ids) if update_id is None else update_id


def _user(
    user_id: int,
    language_code: str | None,
    *,
    is_bot: bool = False,
    first_name: str = "Ann",
) -> User:
    return User(
        id=user_id,
        is_bot=is_bot,
        first_name=first_name,
        username=f"user{user_id}",
        language_code=language_code,
    )


def _chat(chat_id: int, chat_type: str) -> Chat:
    return Chat(
        id=chat_id, type=chat_type, title=None if chat_type == "private" else "Group"
    )


def build_message_update(
    text: str = "hi",
    *,
    user_id: int = 42,
    first_name: str = "Ann",
    chat_id: int | None = None,
    chat_type: str = "private",
    language_code: str | None = "en",
    is_bot: bool = False,
    update_id: int | None = None,
) -> Update:
    message = Message(
        message_id=next(_message_ids),
        date=_NOW,
        chat=_chat(user_id if chat_id is None else chat_id, chat_type),
        from_user=_user(user_id, language_code, is_bot=is_bot, first_name=first_name),
        text=text,
    )
    return Update(update_id=_resolve_update_id(update_id), message=message)


def build_callback_update(
    data: str,
    *,
    user_id: int = 42,
    language_code: str | None = "en",
    message_text: str = "menu",
    update_id: int | None = None,
) -> Update:
    message = Message(
        message_id=next(_message_ids),
        date=_NOW,
        chat=_chat(user_id, "private"),
        from_user=bot_user(),
        text=message_text,
    )
    callback = CallbackQuery(
        id=str(next(_update_ids)),
        from_user=_user(user_id, language_code),
        chat_instance="instance",
        message=message,
        data=data,
    )
    return Update(update_id=_resolve_update_id(update_id), callback_query=callback)


def _member_chat_id(user_id: int, chat_id: int | None, chat_type: str) -> int:
    if chat_id is not None:
        return chat_id
    return user_id if chat_type == "private" else -user_id


def build_my_chat_member_update(
    status: Literal["kicked", "member"],
    *,
    user_id: int = 42,
    chat_id: int | None = None,
    chat_type: str = "private",
    language_code: str | None = "en",
    update_id: int | None = None,
) -> Update:
    bot = bot_user()
    old, new = (
        (ChatMemberMember(user=bot), ChatMemberBanned(user=bot, until_date=0))
        if status == "kicked"
        else (
            ChatMemberBanned(user=bot, until_date=0),
            ChatMemberMember(user=bot),
        )
    )
    event = ChatMemberUpdated(
        chat=_chat(_member_chat_id(user_id, chat_id, chat_type), chat_type),
        from_user=_user(user_id, language_code),
        date=_NOW,
        old_chat_member=old,
        new_chat_member=new,
    )
    return Update(update_id=_resolve_update_id(update_id), my_chat_member=event)
