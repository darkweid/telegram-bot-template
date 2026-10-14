from src.core.errors.exceptions import CoreException
from src.core.i18n import N_


class UnsupportedLocaleError(CoreException):
    """A language callback named a locale we have no catalog for: a stale
    keyboard from an older release, or forged callback data."""

    message_key = N_("This language is not available.")
    log_level = "warning"
