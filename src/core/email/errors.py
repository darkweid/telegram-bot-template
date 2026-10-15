from src.core.errors.exceptions import InfrastructureException


class EmailDisabled(InfrastructureException):
    """A send reached the mailer, or a queue request EmailService, while
    EMAIL_ENABLED is false."""
