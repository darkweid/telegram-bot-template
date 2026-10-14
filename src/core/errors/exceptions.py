from typing import Any, ClassVar, Literal

from aiogram.utils.i18n import gettext as _

from src.core.i18n import N_

LogLevel = Literal["debug", "info", "warning", "error", "critical"]


class CoreException(Exception):
    """Base project exception.

    Subclasses declare how the bot reacts as class attributes; the dispatcher's
    error handler (src/core/telegram/errors.py) treats every one of them the
    same way, so adding an error type means adding a subclass, not a handler.

    `message_key` is the msgid of what the user is told - mark it with N_ so
    `make i18n-extract` finds it. `detail` goes to the log line only and never
    reaches the user; `params` fill the placeholders of `message_key`.
    """

    message_key: ClassVar[str] = N_("Something went wrong. Please try again later.")
    log_level: ClassVar[LogLevel] = "info"
    capture_to_sentry: ClassVar[bool] = False

    def __init__(self, detail: str | None = None, **params: Any) -> None:
        super().__init__(detail or type(self).__name__)
        self.detail = detail
        self.params = params

    def user_message(self) -> str:
        """The text for the user, translated into the locale active right now."""
        return _(self.message_key).format(**self.params)


class InfrastructureException(CoreException):
    """Something we depend on failed in a way the user cannot fix."""

    log_level = "error"
    capture_to_sentry = True


class FilteringError(CoreException):
    """A list query named a field or operator the repository does not allow."""

    log_level = "warning"
