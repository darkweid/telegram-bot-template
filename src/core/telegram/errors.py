"""What the bot does when a handler raises."""

from aiogram import Bot, Router
from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import ErrorEvent
from aiogram.utils.i18n import I18n
from dishka import FromDishka
import sentry_sdk

from loggers import get_logger
from src.core.errors.exceptions import CoreException, format_html
from src.core.errors.reporting import report_core_exception
from src.core.errors.scrubbed import reportable
from src.core.i18n import resolve_locale
from src.core.telegram.alerts import Alert, DeveloperAlerter
from src.core.telegram.context import sentry_scope_for, update_context_from
from src.core.update_context import bind_update_context

logger = get_logger(__name__)


async def handle_error(
    event: ErrorEvent,
    bot: Bot,
    i18n: FromDishka[I18n],
    alerter: FromDishka[DeveloperAlerter],
) -> bool:
    """Log, report and answer one handler failure; an unexpected one also
    alerts the developers.

    Runs from aiogram's ErrorsMiddleware, outside every middleware of ours: the
    update context and the stored user are gone, so the context is rebuilt from
    the update and the reply language comes from Telegram's `language_code`.
    """
    update, error = event.update, event.exception
    context = update_context_from(update)
    alert: Alert | None = None
    with bind_update_context(context), sentry_scope_for(context):
        if isinstance(error, TelegramForbiddenError):
            # The user can no longer be reached (ForbiddenMiddleware records
            # it) or the bot was removed from the chat: there is nobody to
            # tell and nothing to fix.
            logger.info("Not delivered: %s", error.message)
            return True

        if isinstance(error, CoreException):
            report_core_exception(logger, error)
        else:
            safe = reportable(error)
            logger.error("Unhandled %s", type(error).__name__, exc_info=safe)
            event_id = sentry_sdk.capture_exception(safe)
            alert = Alert(
                safe,
                origin=f"update {context.update_type}",
                user_id=context.user_id,
                sentry_event_id=event_id,
            )

        if context.chat_id is not None:
            sender = UserContextMiddleware.resolve_event_context(event=update).user
            locale = resolve_locale(
                sender.language_code if sender else None, default=i18n.default_locale
            )
            try:
                await bot.send_message(
                    context.chat_id, error_reply_text(i18n, locale, error)
                )
            except TelegramAPIError as reply_error:
                logger.warning(
                    "Could not tell the user about the error: %s", reply_error.message
                )
        if alert is not None:
            # After the reply: the user is not kept waiting on the alert chat.
            await alerter.alert_error(alert)
    return True


def error_reply_text(i18n: I18n, locale: str, error: BaseException) -> str:
    """What the user is told about `error` in `locale`: a CoreException's own
    text with its params escaped for HTML, the generic text for anything else.

    Shared by the update's error handler and ProcessingStatus, so a failure
    reads the same whether it happened in an update or in background work.
    """
    if isinstance(error, CoreException):
        message_key, params = error.message_key, error.params
    else:
        message_key, params = CoreException.message_key, {}
    template = i18n.gettext(message_key, locale=locale)
    try:
        return format_html(template, params)
    except (KeyError, IndexError, AttributeError, ValueError, TypeError):
        # A param is missing, lacks an attribute the placeholder reads or
        # does not fit its format spec: the generic text still reaches the
        # user. Params can hold what they typed, so only the key is logged,
        # not the error.
        logger.warning(
            "Cannot format %r with its params; replying generically", message_key
        )
        return i18n.gettext(CoreException.message_key, locale=locale)


def build_errors_router() -> Router:
    """A new router per dispatcher.

    aiogram attaches a router to one parent only, and tests build a
    dispatcher each.
    """
    router = Router(name="errors")
    router.error.register(handle_error)
    return router
