"""What the bot does when a handler raises."""

import logging

from aiogram import Bot, Router
from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import ErrorEvent
from aiogram.utils.i18n import I18n
from dishka import FromDishka
import sentry_sdk

from loggers import get_logger
from src.core.errors.exceptions import CoreException, format_html
from src.core.errors.scrubbed import reportable
from src.core.i18n import resolve_locale
from src.core.telegram.context import sentry_scope_for, update_context_from
from src.core.update_context import bind_update_context

logger = get_logger(__name__)

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


async def handle_error(event: ErrorEvent, bot: Bot, i18n: FromDishka[I18n]) -> bool:
    """Log, report and answer one handler failure.

    Runs from aiogram's ErrorsMiddleware, outside every middleware of ours: the
    update context and the stored user are gone, so the context is rebuilt from
    the update and the reply language comes from Telegram's `language_code`.
    """
    update, error = event.update, event.exception
    context = update_context_from(update)
    with bind_update_context(context), sentry_scope_for(context):
        if isinstance(error, TelegramForbiddenError):
            # The user can no longer be reached (ForbiddenMiddleware records
            # it) or the bot was removed from the chat: there is nobody to
            # tell and nothing to fix.
            logger.info("Not delivered: %s", error.message)
            return True

        if isinstance(error, CoreException):
            level = _LEVELS[error.log_level]
            safe = reportable(error)
            logger.log(
                level,
                "%s: %s",
                type(error).__name__,
                error.detail or "-",
                exc_info=safe if level >= logging.ERROR else None,
            )
            if error.capture_to_sentry:
                sentry_sdk.capture_exception(safe)
            message_key, params = error.message_key, error.params
        else:
            safe = reportable(error)
            logger.error("Unhandled %s", type(error).__name__, exc_info=safe)
            sentry_sdk.capture_exception(safe)
            message_key, params = CoreException.message_key, {}

        if context.chat_id is not None:
            sender = UserContextMiddleware.resolve_event_context(event=update).user
            locale = resolve_locale(
                sender.language_code if sender else None, default=i18n.default_locale
            )
            template = i18n.gettext(message_key, locale=locale)
            try:
                text = format_html(template, params)
            except (KeyError, IndexError, AttributeError, ValueError, TypeError):
                # A param is missing, lacks an attribute the placeholder reads
                # or does not fit its format spec: the generic text still
                # reaches the user. Params can hold what they typed, so only
                # the key is logged, not the error.
                logger.warning(
                    "Cannot format %r with its params; replying generically",
                    message_key,
                )
                text = i18n.gettext(CoreException.message_key, locale=locale)
            try:
                await bot.send_message(context.chat_id, text)
            except TelegramAPIError as reply_error:
                logger.warning(
                    "Could not tell the user about the error: %s", reply_error.message
                )
    return True


def build_errors_router() -> Router:
    """A new router per dispatcher.

    aiogram attaches a router to one parent only, and tests build a
    dispatcher each.
    """
    router = Router(name="errors")
    router.error.register(handle_error)
    return router
