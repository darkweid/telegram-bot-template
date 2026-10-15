from datetime import UTC, datetime
from unittest.mock import AsyncMock

from aiogram import Bot, Router
from aiogram.dispatcher.middlewares.error import ErrorsMiddleware
from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.filters import Command
from aiogram.fsm.middleware import FSMContextMiddleware
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from aiogram.utils.i18n import I18n
from dishka import FromDishka, Provider, Scope, provide
from dishka.integrations.aiogram import ContainerMiddleware

from src.core.i18n.middleware import UserLocaleMiddleware
from src.core.telegram.middlewares.callback_answer import (
    SkipAwareCallbackAnswerMiddleware,
)
from src.core.telegram.middlewares.throttling import ThrottlingMiddleware
from src.core.telegram.middlewares.update_context import UpdateContextMiddleware
from src.core.telegram.roles import Roles
from src.main.config import Config
from src.main.dispatcher import create_dispatcher
from src.user.middlewares import BanMiddleware, UserMiddleware
from src.user.schemas import UserProfile
from src.user.usecases.touch_user import TouchUserUseCase
from tests.factories.updates import build_callback_update, build_message_update
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.dispatcher import running_bot
from tests.helpers.providers import build_test_container


async def test_middlewares_run_in_the_documented_order(
    settings: Config, i18n: I18n
) -> None:
    async with running_bot(settings, i18n) as running:
        assert [type(m) for m in running.dp.update.outer_middleware] == [
            ErrorsMiddleware,
            UserContextMiddleware,
            FSMContextMiddleware,
            UpdateContextMiddleware,
            ContainerMiddleware,
            UserMiddleware,
            UserLocaleMiddleware,
            BanMiddleware,
        ]
        assert [type(m) for m in running.dp.callback_query.middleware] == [
            SkipAwareCallbackAnswerMiddleware,
            ThrottlingMiddleware,
        ]
        assert [type(m) for m in running.dp.message.middleware] == [
            ThrottlingMiddleware
        ]
        assert isinstance(running.dp.fsm.storage, MemoryStorage)


async def test_the_bot_asks_only_for_the_updates_it_handles(
    settings: Config, i18n: I18n
) -> None:
    async with running_bot(settings, i18n) as running:
        assert set(running.dp.resolve_used_update_types()) == {
            "message",
            "callback_query",
            "my_chat_member",
        }


async def test_two_dispatchers_can_be_built_in_one_process(
    settings: Config, i18n: I18n
) -> None:
    async with running_bot(settings, i18n), running_bot(settings, i18n):
        pass  # a module-level router would raise "already attached" here


async def test_the_container_opens_scopes_on_updates_and_errors_only(
    settings: Config, i18n: I18n
) -> None:
    async with running_bot(settings, i18n) as running:
        with_container = {
            name
            for name, observer in running.dp.observers.items()
            if any(
                isinstance(m, ContainerMiddleware) for m in observer.outer_middleware
            )
        }
        assert with_container == {"update", "error"}


class CountingTouch(Provider):
    """TouchUserUseCase that counts how many request scopes built it."""

    def __init__(self) -> None:
        super().__init__()
        self.built = 0
        self.touch = AsyncMock(spec=TouchUserUseCase)
        self.touch.execute.return_value = UserProfile(
            id=7,
            first_name="Ann",
            username=None,
            last_name=None,
            language_code="en",
            locale=None,
            is_reachable=True,
            is_banned=False,
            last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    @provide(scope=Scope.REQUEST, override=True)
    def touch_user(self) -> TouchUserUseCase:
        self.built += 1
        return self.touch


async def test_middlewares_and_the_handler_share_one_request_scope(
    settings: Config, i18n: I18n
) -> None:
    counting = CountingTouch()
    container = build_test_container(settings, FakeTelegramSession(), i18n, counting)
    try:
        dp = await create_dispatcher(container)
        probe = Router()
        resolved: list[TouchUserUseCase] = []

        @probe.message(Command("probe"))
        async def handler(
            message: Message, touch: FromDishka[TouchUserUseCase]
        ) -> None:
            resolved.append(touch)

        dp.include_router(probe)
        bot = await container.get(Bot)
        await dp.emit_startup(bot=bot)
        try:
            await dp.feed_update(bot, build_message_update("/probe", user_id=7))
        finally:
            await dp.emit_shutdown(bot=bot)
    finally:
        await container.close()
    # UserMiddleware and the handler asked for it; one scope built it once.
    assert resolved == [counting.touch]
    assert counting.built == 1


async def test_a_callback_no_handler_matches_is_still_answered(
    settings: Config, i18n: I18n
) -> None:
    async with running_bot(settings, i18n, CountingTouch()) as running:
        await running.feed(build_callback_update("unknown:1"))
        assert len(running.session.sent(AnswerCallbackQuery)) == 1


async def test_a_callback_from_an_inline_message_is_still_answered(
    settings: Config, i18n: I18n
) -> None:
    callback = CallbackQuery(
        id="1",
        from_user=User(id=42, is_bot=False, first_name="Ann"),
        chat_instance="instance",
        inline_message_id="inline-1",
        data="unknown:1",
    )
    async with running_bot(settings, i18n, CountingTouch()) as running:
        await running.feed(Update(update_id=999_003, callback_query=callback))
        assert len(running.session.sent(AnswerCallbackQuery)) == 1


async def test_a_callback_from_a_group_is_still_answered(
    settings: Config, i18n: I18n
) -> None:
    callback = CallbackQuery(
        id="1",
        from_user=User(id=42, is_bot=False, first_name="Ann"),
        chat_instance="instance",
        message=Message(
            message_id=1,
            date=datetime(2026, 1, 1, tzinfo=UTC),
            chat=Chat(id=-100123, type="supergroup"),
            text="menu",
        ),
        data="unknown:1",
    )
    async with running_bot(settings, i18n, CountingTouch()) as running:
        await running.feed(Update(update_id=999_004, callback_query=callback))
        assert len(running.session.sent(AnswerCallbackQuery)) == 1


async def test_roles_reach_every_update(settings: Config, i18n: I18n) -> None:
    """Role filters read `roles` from the update's data; without it every
    admin command would fail with a TypeError instead of running."""
    async with running_bot(settings, i18n) as running:
        assert isinstance(running.dp["roles"], Roles)


async def test_role_routers_come_before_the_user_router(
    settings: Config, i18n: I18n
) -> None:
    """An admin's command that a user handler also matches (/help) must reach
    the admin handler first."""
    async with running_bot(settings, i18n) as running:
        names = [router.name for router in running.dp.sub_routers]
    assert names.index("admin") < names.index("user")
    assert names.index("developer") < names.index("user")
    assert names.index("broadcast") < names.index("user")
