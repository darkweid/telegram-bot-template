from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Message
from aiogram.utils.i18n import I18n
from dishka import Provider, Scope, provide
import pytest

from src.core.limiter.limiter import RateLimiter
from src.core.redis.degradation import RedisDegradationReporter
from src.core.telegram.middlewares.throttling import (
    Throttle,
    ThrottleRule,
    ThrottlingMiddleware,
)
from src.core.telegram.roles import Roles
from src.main.config import Config, ThrottlingConfig
from src.user.callbacks import LanguageCallback
from src.user.usecases.set_locale import SetLocaleUseCase
from tests.factories.updates import build_callback_update, build_message_update
from tests.fakes.redis import InMemoryRedis
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.dispatcher import running_bot
from tests.helpers.runtime import Stubs

ADMIN_ID = 7


def _tight(settings: Config, *, admin: int | None = None) -> Config:
    """Two updates per minute; `admin` joins ADMIN_IDS."""
    bot = settings.bot
    if admin is not None:
        bot = bot.model_copy(update={"ADMIN_IDS": frozenset({admin})})
    return settings.model_copy(
        update={
            "bot": bot,
            "throttling": ThrottlingConfig(
                THROTTLE_DEFAULT_LIMIT=2, THROTTLE_DEFAULT_WINDOW_SECONDS=60
            ),
        }
    )


class SetLocaleStub(Provider):
    """The language button's use case, kept off the database."""

    @provide(scope=Scope.REQUEST, override=True)
    def set_locale(self) -> SetLocaleUseCase:
        return AsyncMock(spec=SetLocaleUseCase)


async def _help_replies(
    config: Config, i18n: I18n, count: int, user_id: int = 42
) -> list[str]:
    async with running_bot(config, i18n, Stubs(FakeTelegramSession())) as running:
        for _ in range(count):
            await running.feed(build_message_update("/help", user_id=user_id))
        return [reply.text or "" for reply in running.session.sent(SendMessage)]


async def test_a_flood_gets_one_warning_then_silence(
    settings: Config, i18n: I18n
) -> None:
    """Answering every refused update would let a flood make the bot flood back."""
    replies = await _help_replies(_tight(settings), i18n, 5)
    assert len(replies) == 3
    assert "/start" in replies[0] and "/start" in replies[1]
    assert replies[2].startswith("Too many requests")


async def test_admins_are_never_throttled(settings: Config, i18n: I18n) -> None:
    """A moderator caught by the flood limit could not stop the flood."""
    replies = await _help_replies(_tight(settings, admin=ADMIN_ID), i18n, 5, ADMIN_ID)
    assert len(replies) == 5


async def test_a_throttled_callback_is_answered_once_with_the_warning(
    settings: Config, i18n: I18n
) -> None:
    """The callback middleware answers every query; a second answer from
    throttling would be refused by Telegram."""
    async with running_bot(
        _tight(settings), i18n, Stubs(FakeTelegramSession()), SetLocaleStub()
    ) as running:
        for _ in range(4):
            await running.feed(
                build_callback_update(LanguageCallback(code="en").pack())
            )
        answers = running.session.sent(AnswerCallbackQuery)
    assert len(answers) == 4
    texts = [answer.text or "" for answer in answers]
    assert not any(text.startswith("Too many requests") for text in texts[:2])
    assert texts[2].startswith("Too many requests")
    assert not texts[3].startswith("Too many requests")


async def _llm_router_replies(limiter: RateLimiter, i18n: I18n, count: int) -> int:
    router = Router()

    @router.message(Command("ask"), flags={"throttle": Throttle.LLM})
    async def ask(message: Message) -> None:
        await message.answer("answer")

    middleware = ThrottlingMiddleware(
        limiter,
        {
            Throttle.DEFAULT: ThrottleRule(limit=100, window_seconds=60),
            Throttle.LLM: ThrottleRule(limit=1, window_seconds=60),
        },
    )
    dp = Dispatcher(roles=Roles(admins=frozenset(), developers=frozenset()))
    dp.message.middleware(middleware)
    dp.include_router(router)
    session = FakeTelegramSession()
    bot = Bot("42:" + "A" * 35, session=session)
    try:
        # A bare Dispatcher has no I18n middleware; the refusal text needs a
        # current locale.
        with i18n.context(), i18n.use_locale("en"):
            for _ in range(count):
                await dp.feed_update(bot, build_message_update("/ask"))
        return sum(reply.text == "answer" for reply in session.sent(SendMessage))
    finally:
        await bot.session.close()


async def test_a_handler_flag_picks_the_stricter_profile(i18n: I18n) -> None:
    """A paid-model handler must not share the generous default budget."""
    limiter = RateLimiter(
        InMemoryRedis(),
        prefix="throttle",
        reporter=RedisDegradationReporter("Throttling"),
    )
    assert await _llm_router_replies(limiter, i18n, 3) == 1


async def test_limits_hold_while_redis_is_down(i18n: I18n) -> None:
    """Fail open would hand a flood straight to the LLM bill."""
    redis = InMemoryRedis()
    redis.fail_next_commands(100)
    limiter = RateLimiter(
        redis,
        prefix="throttle",
        reporter=RedisDegradationReporter("Throttling"),
    )
    assert await _llm_router_replies(limiter, i18n, 3) == 1


async def test_an_unknown_profile_is_a_programming_error() -> None:
    """A profile without a rule would otherwise fail on some user's first update."""
    limiter = RateLimiter(
        InMemoryRedis(),
        prefix="throttle",
        reporter=RedisDegradationReporter("Throttling"),
    )
    with pytest.raises(ValueError, match="rule"):
        ThrottlingMiddleware(limiter, {Throttle.DEFAULT: ThrottleRule(1, 60)})


async def test_a_skipped_handler_counts_once_per_handler_reached(i18n: I18n) -> None:
    """aiogram re-runs inner middlewares for the next matching handler after
    SkipHandler; the limit counts invocations, and the docstring says so."""
    router = Router()
    reached: list[str] = []

    @router.message(Command("ask"))
    async def skipping(message: Message) -> None:
        reached.append("skipping")
        raise SkipHandler

    @router.message(Command("ask"))
    async def answering(message: Message) -> None:
        reached.append("answering")

    limiter = RateLimiter(
        InMemoryRedis(),
        prefix="throttle",
        reporter=RedisDegradationReporter("Throttling"),
    )
    rule = ThrottleRule(limit=1, window_seconds=60)
    dp = Dispatcher(roles=Roles(admins=frozenset(), developers=frozenset()))
    dp.message.middleware(
        ThrottlingMiddleware(limiter, {Throttle.DEFAULT: rule, Throttle.LLM: rule})
    )
    dp.include_router(router)
    bot = Bot("42:" + "A" * 35, session=FakeTelegramSession())
    try:
        with i18n.context(), i18n.use_locale("en"):
            await dp.feed_update(bot, build_message_update("/ask"))
    finally:
        await bot.session.close()
    assert reached == ["skipping"]
