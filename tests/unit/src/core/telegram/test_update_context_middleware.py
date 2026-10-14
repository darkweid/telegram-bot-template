import asyncio
from typing import Any

from aiogram import Dispatcher
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendChatAction, SendMessage
from aiogram.types import Message, TelegramObject
import pytest
import sentry_sdk

from src.core.telegram.middlewares.update_context import UpdateContextMiddleware
from src.core.telegram.session import MAX_RETRY_SLEEP_SECONDS, RetryAfterMiddleware
from src.core.update_context import (
    UpdateContext,
    bind_update_context,
    get_update_context,
)
from tests.factories.updates import build_message_update
from tests.fakes.telegram import FakeTelegramSession, make_bot
from tests.helpers.sentry import scope_tags, scope_user


async def test_handlers_see_the_update_context_and_it_is_gone_afterwards() -> None:
    seen: list[UpdateContext | None] = []
    dp = Dispatcher()
    dp.update.outer_middleware(UpdateContextMiddleware())

    @dp.message()
    async def handler(message: Message) -> None:
        seen.append(get_update_context())

    await dp.feed_update(
        make_bot(FakeTelegramSession()), build_message_update(user_id=7, update_id=3)
    )
    assert seen == [
        UpdateContext(update_id=3, update_type="message", user_id=7, chat_id=7)
    ]
    assert get_update_context() is None


async def test_a_failing_handler_leaves_no_context_and_no_scope_behind() -> None:
    outer = sentry_sdk.get_current_scope()
    update = build_message_update(user_id=7, update_id=3)

    async def failing(event: TelegramObject, data: dict[str, Any]) -> Any:
        assert get_update_context() is not None
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await UpdateContextMiddleware()(failing, update, {})

    assert get_update_context() is None
    assert "update_type" not in scope_tags(sentry_sdk.get_current_scope())
    assert sentry_sdk.get_current_scope() is outer


async def test_concurrent_updates_each_see_only_their_own_context() -> None:
    dp = Dispatcher()
    dp.update.outer_middleware(UpdateContextMiddleware())
    both_inside = [asyncio.Event(), asyncio.Event()]
    observed: dict[int, list[tuple[UpdateContext | None, dict[str, Any] | None]]] = {}

    @dp.message()
    async def handler(message: Message) -> None:
        assert message.from_user is not None
        user_id = message.from_user.id
        index = 0 if user_id == 1 else 1
        snapshots = observed.setdefault(user_id, [])
        scope = sentry_sdk.get_current_scope()
        snapshots.append((get_update_context(), scope_user(scope)))
        both_inside[index].set()
        await both_inside[1 - index].wait()  # forces the two handlers to interleave
        await asyncio.sleep(0)
        snapshots.append(
            (get_update_context(), scope_user(sentry_sdk.get_current_scope()))
        )

    bot = make_bot(FakeTelegramSession())
    await asyncio.gather(
        dp.feed_update(bot, build_message_update(user_id=1, update_id=101)),
        dp.feed_update(bot, build_message_update(user_id=2, update_id=102)),
    )

    for user_id, update_id in ((1, 101), (2, 102)):
        context = UpdateContext(
            update_id=update_id, update_type="message", user_id=user_id, chat_id=user_id
        )
        assert observed[user_id] == [(context, {"id": str(user_id)})] * 2
    assert get_update_context() is None


def test_a_nested_bind_restores_the_outer_context_on_exit() -> None:
    outer = UpdateContext(update_id=1, update_type="message", user_id=1, chat_id=1)
    inner = UpdateContext(update_id=2, update_type="message", user_id=2, chat_id=2)
    with bind_update_context(outer):
        with bind_update_context(inner):
            assert get_update_context() == inner
        assert get_update_context() == outer
    assert get_update_context() is None


async def test_an_event_that_is_not_an_update_passes_through_unbound() -> None:
    seen: list[UpdateContext | None] = []
    event = build_message_update().message
    assert event is not None

    async def handler(event: TelegramObject, data: dict[str, Any]) -> str:
        seen.append(get_update_context())
        return "handled"

    assert await UpdateContextMiddleware()(handler, event, {}) == "handled"
    assert seen == [None]


async def test_each_update_gets_one_flood_wait_budget() -> None:
    session, sleeps = FakeTelegramSession(), []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    session.middleware(RetryAfterMiddleware(attempts=3, sleep=sleep))
    wait = MAX_RETRY_SLEEP_SECONDS // 2 + 1
    flood = TelegramRetryAfter(
        SendMessage(chat_id=7, text="x"), "Too Many Requests", wait
    )
    session.queue(SendMessage, flood)
    session.queue(SendChatAction, flood)
    session.queue(SendChatAction, flood)
    outcomes: list[str] = []
    dp = Dispatcher()
    dp.update.outer_middleware(UpdateContextMiddleware())

    @dp.message()
    async def handler(message: Message) -> None:
        await message.answer("x")
        try:
            await message.bot.send_chat_action(message.chat.id, "typing")
            outcomes.append("sent")
        except TelegramRetryAfter:
            outcomes.append("flood")

    bot = make_bot(session)
    await dp.feed_update(bot, build_message_update(user_id=7, update_id=1))
    await dp.feed_update(bot, build_message_update(user_id=7, update_id=2))
    # In the first update the second wait does not fit what the first left;
    # the second update fits the same wait, so its budget is its own.
    assert outcomes == ["flood", "sent"]
    assert sleeps == [wait, wait]
