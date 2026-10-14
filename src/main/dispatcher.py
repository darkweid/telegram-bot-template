from functools import partial

from aiogram import Dispatcher, Router
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.types import CallbackQuery
from aiogram.utils.i18n import I18n
from dishka import AsyncContainer
from dishka.integrations.aiogram import ContainerMiddleware, inject_router

from src.admin.handlers import build_admin_router
from src.broadcast.handlers import build_broadcast_router
from src.core.i18n.middleware import UserLocaleMiddleware
from src.core.telegram.errors import build_errors_router
from src.core.telegram.middlewares.callback_answer import (
    SkipAwareCallbackAnswerMiddleware,
)
from src.core.telegram.middlewares.throttling import ThrottlingMiddleware
from src.core.telegram.middlewares.update_context import UpdateContextMiddleware
from src.core.telegram.roles import Roles
from src.developer.handlers import build_developer_router
from src.user.handlers import build_user_router
from src.user.middlewares import BanMiddleware, UserMiddleware


async def create_dispatcher(container: AsyncContainer) -> Dispatcher:
    """The dispatcher both polling and webhook run.

    Outer middlewares on `update` run in registration order, after aiogram's
    own (errors, user context, FSM context):
    1. UpdateContextMiddleware - ids for logs and Sentry and the update's
       flood-wait budget, for everything below;
    2. dishka's ContainerMiddleware - one request scope per update, which the
       handler shares;
    3. UserMiddleware - stores the sender, needs the request scope;
    4. UserLocaleMiddleware - needs the stored user's chosen language;
    5. BanMiddleware - drops a banned user's updates; a role beats a ban.

    Inner middlewares on message and callback_query: the callback answer
    middleware, then ThrottlingMiddleware.

    Role routers come before the user router: an admin's command must reach the
    admin handler even when a user handler would match it too.
    """
    dp = Dispatcher(
        storage=await container.get(BaseStorage),
        events_isolation=await container.get(BaseEventIsolation),
        roles=await container.get(Roles),
    )
    container_middleware = ContainerMiddleware(container)
    dp.update.outer_middleware(UpdateContextMiddleware())
    dp.update.outer_middleware(container_middleware)
    dp.update.outer_middleware(UserMiddleware())
    dp.update.outer_middleware(UserLocaleMiddleware(await container.get(I18n)))
    dp.update.outer_middleware(BanMiddleware())
    # Error handlers run from aiogram's ErrorsMiddleware, after the update's
    # scope has closed, so they get a scope of their own.
    dp.errors.outer_middleware(container_middleware)
    # Wrap every FromDishka handler once, when the dispatcher starts.
    dp.startup.register(partial(inject_router, router=dp))
    # Answers every callback query, so the button's spinner stops even when
    # the handler fails; handlers may still answer with text themselves. An
    # inner middleware runs only around a matched handler, so the last router
    # matches whatever no other router took: a stale button, a group chat or
    # an inline message.
    dp.callback_query.middleware(SkipAwareCallbackAnswerMiddleware())
    # Inner, after the callback answer middleware: a throttled callback still
    # gets its one answer, carrying the warning.
    throttling = await container.get(ThrottlingMiddleware)
    dp.message.middleware(throttling)
    dp.callback_query.middleware(throttling)
    dp.include_routers(
        build_errors_router(),
        build_admin_router(),
        build_developer_router(),
        build_broadcast_router(),
        build_user_router(),
        _build_unmatched_router(),
    )
    return dp


def _build_unmatched_router() -> Router:
    router = Router(name="unmatched")
    router.callback_query.register(_ignore_callback)
    return router


async def _ignore_callback(callback: CallbackQuery) -> None:
    return None
