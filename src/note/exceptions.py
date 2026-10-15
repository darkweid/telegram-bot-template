from src.core.errors.exceptions import CoreException
from src.core.i18n import N_


class EmptyNoteError(CoreException):
    message_key = N_("A note cannot be empty.")
    log_level = "info"


class NoteTooLongError(CoreException):
    """Raised with `limit` and `length`; the text itself is never kept."""

    message_key = N_(
        "A note can be at most {limit} characters long. This one has {length}."
    )
    log_level = "info"


class NoteNotFoundError(CoreException):
    """The note is gone, or it belongs to someone else: a forged or stale
    button cannot tell the two apart, and the bot does not either."""

    message_key = N_("This note no longer exists.")
    log_level = "info"


class NoteSummaryFailedError(CoreException):
    """The model's answer was cut at max_tokens or failed validation after
    the repair attempt; the cause is chained for Sentry."""

    message_key = N_("The summary did not work out this time. Please try again.")
    log_level = "warning"
    capture_to_sentry = True
