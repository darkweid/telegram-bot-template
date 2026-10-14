from functools import partial

from aiogram import Dispatcher
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.utils.callback_answer import CallbackAnswerMiddleware
from aiogram.utils.i18n import I18n
from dishka import AsyncContainer
from dishka.integrations.aiogram import ContainerMiddleware, inject_router

from src.core.i18n.middleware import UserLocaleMiddleware
from src.core.telegram.errors import build_errors_router
from src.core.telegram.middlewares.update_context import UpdateContextMiddleware
from src.user.handlers import build_user_router
from src.user.middlewares import UserMiddleware


async def create_dispatcher(container: AsyncContainer) -> Dispatcher:
    """The dispatcher both polling and webhook run.

    Outer middlewares on `update` run in registration order, after aiogram's
    own (errors, user context, FSM context):
    1. UpdateContextMiddleware - ids for logs and Sentry, for everything below;
    2. dishka's ContainerMiddleware - one request scope per update, which the
       handler shares;
    3. UserMiddleware - stores the sender, needs the request scope;
    4. UserLocaleMiddleware - needs the stored user's chosen language.
    """
    dp = Dispatcher(
        storage=await container.get(BaseStorage),
        events_isolation=await container.get(BaseEventIsolation),
    )
    container_middleware = ContainerMiddleware(container)
    dp.update.outer_middleware(UpdateContextMiddleware())
    dp.update.outer_middleware(container_middleware)
    dp.update.outer_middleware(UserMiddleware())
    dp.update.outer_middleware(UserLocaleMiddleware(await container.get(I18n)))
    # Error handlers run from aiogram's ErrorsMiddleware, after the update's
    # scope has closed, so they get a scope of their own.
    dp.errors.outer_middleware(container_middleware)
    # Wrap every FromDishka handler once, when the dispatcher starts.
    dp.startup.register(partial(inject_router, router=dp))
    # Answers every callback query, so the button's spinner stops even when
    # the handler fails; handlers may still answer with text themselves.
    dp.callback_query.middleware(CallbackAnswerMiddleware())
    dp.include_routers(build_errors_router(), build_user_router())
    return dp
