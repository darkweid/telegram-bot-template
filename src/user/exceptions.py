from src.core.errors.exceptions import CoreException
from src.core.i18n import N_


class UnsupportedLocaleError(CoreException):
    """A language callback named a locale we have no catalog for: a stale
    keyboard from an older release, or forged callback data."""

    message_key = N_("This language is not available.")
    log_level = "warning"


class UserNotFoundError(CoreException):
    """The command named a user the bot has never stored."""

    message_key = N_("No such user. They have to start the bot first.")
    log_level = "info"


class AmbiguousUsernameError(CoreException):
    """Two stored users share the username: one of the rows is stale."""

    message_key = N_("Several users have had @{username}. Use the numeric id instead.")
    log_level = "info"


class ProtectedUserError(CoreException):
    message_key = N_("Admins and developers cannot be banned.")
    log_level = "info"


class SelfBanError(CoreException):
    message_key = N_("You cannot ban yourself.")
    log_level = "info"
