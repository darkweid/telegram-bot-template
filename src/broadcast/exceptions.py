from src.core.errors.exceptions import CoreException
from src.core.i18n import N_


class BroadcastInProgressError(CoreException):
    """Another broadcast is pending or running; one at a time."""

    message_key = N_(
        "A broadcast is already running. Wait for it to finish or stop it."
    )
    log_level = "info"
