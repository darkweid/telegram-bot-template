from aiogram import Dispatcher, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, Message

from src.core.telegram.middlewares.callback_answer import (
    SkipAwareCallbackAnswerMiddleware,
)
from tests.factories.updates import build_callback_update, build_message_update
from tests.fakes.telegram import FakeTelegramSession, make_bot


def _dispatcher(*routers: Router) -> Dispatcher:
    dp = Dispatcher()
    dp.callback_query.middleware(SkipAwareCallbackAnswerMiddleware())
    dp.include_routers(*routers)
    return dp


async def test_a_callback_passed_on_with_skip_handler_is_answered_once() -> None:
    first, second = Router(), Router()
    handled: list[str] = []

    @first.callback_query()
    async def passes_on(callback: CallbackQuery) -> None:
        handled.append("first")
        raise SkipHandler()

    @second.callback_query()
    async def takes_it(callback: CallbackQuery) -> None:
        handled.append("second")

    session = FakeTelegramSession()
    await _dispatcher(first, second).feed_update(
        make_bot(session), build_callback_update("x:1")
    )
    assert handled == ["first", "second"]
    assert len(session.sent(AnswerCallbackQuery)) == 1


async def test_a_callback_every_handler_passes_on_is_not_answered() -> None:
    # Nothing handled it here; a catch-all router after these answers it.
    router = Router()

    @router.callback_query()
    async def passes_on(callback: CallbackQuery) -> None:
        raise SkipHandler()

    session = FakeTelegramSession()
    await _dispatcher(router).feed_update(
        make_bot(session), build_callback_update("x:1")
    )
    assert session.sent(AnswerCallbackQuery) == []


async def test_a_handled_callback_is_answered_once() -> None:
    router = Router()

    @router.callback_query()
    async def handles(callback: CallbackQuery) -> None:
        pass

    session = FakeTelegramSession()
    await _dispatcher(router).feed_update(
        make_bot(session), build_callback_update("x:1")
    )
    assert len(session.sent(AnswerCallbackQuery)) == 1


async def test_other_events_pass_through() -> None:
    router = Router()
    seen: list[str | None] = []

    @router.message()
    async def handles(message: Message) -> None:
        seen.append(message.text)

    dp = Dispatcher()
    dp.message.middleware(SkipAwareCallbackAnswerMiddleware())
    dp.include_router(router)
    session = FakeTelegramSession()
    await dp.feed_update(make_bot(session), build_message_update("hi"))
    assert seen == ["hi"]
    assert session.sent(AnswerCallbackQuery) == []
