from src.core.errors.exceptions import CoreException, InfrastructureException
from src.core.i18n import N_


class S3Disabled(InfrastructureException):
    """A call reached file storage while S3_ENABLED is false."""


class S3ObjectTooLarge(CoreException):
    """A stream outgrew S3_MAX_UPLOAD_SIZE_BYTES; nothing was stored."""

    message_key = N_("The file is too large to store.")
