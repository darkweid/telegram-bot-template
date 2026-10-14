"""What the bot does when a handler raises."""

import logging

from aiogram import Bot, Router
from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import ErrorEvent
from aiogram.utils.i18n import I18n
from dishka import FromDishka
from pydantic import ValidationError
import sentry_sdk
from sqlalchemy.exc import DBAPIError

from loggers import get_logger
from src.core.errors.exceptions import CoreException, format_html
from src.core.errors.scrubbed import describe_validation_error, scrubbed
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


def _scrubbed_link(error: BaseException) -> BaseException:
    if isinstance(error, ValidationError):
        # Pydantic chains nothing of the input, so the walk goes on below.
        replacement = scrubbed(describe_validation_error(error), error)
        replacement.__cause__ = error.__cause__
        replacement.__context__ = error.__context__
        replacement.__suppress_context__ = error.__suppress_context__
        return replacement
    if isinstance(error, DBAPIError):
        # Everything below is the driver and its adapter repeating the DETAIL
        # in their messages, so the replacement ends the chain.
        original = type(error.orig).__name__ if error.orig is not None else "unknown"
        return scrubbed(f"{type(error).__name__} from the driver ({original})", error)
    return error


def _reportable(error: BaseException) -> BaseException:
    """The exception as it may appear in a log line or a Sentry event.

    A pydantic error prints the input it rejected; a driver error carries
    PostgreSQL's DETAIL with the row it refused. Both are replaced by a
    summary built from safe fields, keeping the traceback, wherever they are:
    logging and Sentry render the whole `__cause__` and `__context__` chain.
    The chain goes on below a pydantic error and ends at a driver error.
    It is rewritten in place; nothing raises the error after this.
    """
    reportable = _scrubbed_link(error)
    pending = [reportable]
    seen: set[int] = set()
    while pending:
        link = pending.pop()
        if id(link) in seen:
            continue
        seen.add(id(link))
        # Assigning __cause__ also sets __suppress_context__.
        suppress_context = link.__suppress_context__
        if link.__cause__ is not None:
            link.__cause__ = _scrubbed_link(link.__cause__)
            pending.append(link.__cause__)
        if link.__context__ is not None:
            link.__context__ = _scrubbed_link(link.__context__)
            pending.append(link.__context__)
        link.__suppress_context__ = suppress_context
    return reportable


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
            reportable = _reportable(error)
            logger.log(
                level,
                "%s: %s",
                type(error).__name__,
                error.detail or "-",
                exc_info=reportable if level >= logging.ERROR else None,
            )
            if error.capture_to_sentry:
                sentry_sdk.capture_exception(reportable)
            message_key, params = error.message_key, error.params
        else:
            reportable = _reportable(error)
            logger.error("Unhandled %s", type(error).__name__, exc_info=reportable)
            sentry_sdk.capture_exception(reportable)
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
