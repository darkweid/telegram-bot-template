from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

from aiogram import Dispatcher
from aiogram.types import Chat, Message, Update
from aiogram.utils.i18n import I18n
from dishka import Provider, Scope, provide
from dishka.integrations.aiogram import setup_dishka
import pytest

from src.main.config import Config
from src.user.middlewares import UserMiddleware
from src.user.schemas import TelegramProfile, UserProfile
from src.user.usecases.touch_user import TouchUserUseCase
from tests.factories.updates import build_message_update, build_my_chat_member_update
from tests.fakes.telegram import FakeTelegramSession, make_bot
from tests.helpers.providers import build_test_container


def _build_senderless_update() -> Update:
    message = Message(
        message_id=1,
        date=datetime(2026, 1, 1),
        chat=Chat(id=7, type="private"),
        text="hi",
    )
    return Update(update_id=1, message=message)


STORED = UserProfile(
    id=7,
    first_name="Ann",
    username="user7",
    last_name=None,
    language_code="en",
    locale="ru",
    is_reachable=True,
    is_banned=False,
    last_seen_at=datetime(2026, 1, 1),
)


async def _run(
    settings: Config, i18n: I18n, update: Update
) -> tuple[AsyncMock, list[Any]]:
    touch = AsyncMock(spec=TouchUserUseCase)
    touch.execute.return_value = STORED

    class Fake(Provider):
        @provide(scope=Scope.REQUEST, override=True)
        def touch(self) -> TouchUserUseCase:
            return touch

    container = build_test_container(settings, FakeTelegramSession(), i18n, Fake())
    seen: list[Any] = []
    dp = Dispatcher()
    setup_dishka(container, router=dp)
    dp.update.outer_middleware(UserMiddleware())

    @dp.message()
    async def on_message(message: Message, user: UserProfile | None = None) -> None:
        seen.append(user)

    @dp.my_chat_member()
    async def on_member(event: Any, user: UserProfile | None = None) -> None:
        seen.append(user)

    try:
        await dp.feed_update(make_bot(FakeTelegramSession()), update)
    finally:
        await container.close()
    return touch, seen


async def test_private_message_from_a_human_is_touched(
    settings: Config, i18n: I18n
) -> None:
    touch, seen = await _run(settings, i18n, build_message_update(user_id=7))
    assert seen == [STORED]
    profile = touch.execute.await_args.args[0]
    assert isinstance(profile, TelegramProfile) and profile.id == 7


@pytest.mark.parametrize(
    "update",
    [
        build_message_update(user_id=7, chat_id=-100, chat_type="group"),
        build_message_update(user_id=7, is_bot=True),
        build_my_chat_member_update("kicked", user_id=7),
        _build_senderless_update(),
    ],
    ids=["group", "bot-sender", "my-chat-member", "no-sender"],
)
async def test_other_updates_are_not_touched(
    settings: Config, i18n: I18n, update: Update
) -> None:
    touch, seen = await _run(settings, i18n, update)
    touch.execute.assert_not_awaited()
    assert seen == [None]
