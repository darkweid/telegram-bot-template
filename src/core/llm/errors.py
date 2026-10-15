from typing import Any, ClassVar

from src.core.errors.exceptions import CoreException, LogLevel
from src.core.i18n import N_
from src.core.llm.enums import LLMProviderName
from src.core.llm.types import LLMUsage


class LLMError(CoreException):
    """A model call failed. `provider` is the one that failed, None when no
    provider was reached; it goes to the log line with `detail`.

    `usage` is what the provider billed before the call failed: a refusal, an
    answer that failed validation, an answer whose tool call cannot be read,
    or the first answer of a repair whose request then failed. llm_usage and
    the log line record the failure with it; None when nothing was billed.
    """

    message_key = N_("The language model could not answer. Please try again later.")
    log_level: ClassVar[LogLevel] = "error"
    capture_to_sentry = True

    def __init__(
        self,
        detail: str | None = None,
        *,
        provider: LLMProviderName | None = None,
        usage: LLMUsage | None = None,
        **params: Any,
    ) -> None:
        super().__init__(detail, **params)
        self.provider = provider
        self.usage = usage


class LLMNotConfigured(LLMError):
    """LLM_PROVIDER is empty: the feature is off in this deployment."""

    message_key = N_("This feature is not available in this bot.")
    log_level = "warning"
    capture_to_sentry = False


class LLMRateLimited(LLMError):
    """The provider refused for rate or quota after the SDK's own retries."""

    message_key = N_(
        "The language model is busy right now. Please try again in a minute."
    )
    log_level = "warning"
    capture_to_sentry = False


class LLMTimeout(LLMError):
    """No answer within LLM_TIMEOUT_SECONDS on any attempt."""

    message_key = N_("The language model took too long to answer. Please try again.")
    log_level = "warning"
    capture_to_sentry = False


class LLMUnavailable(LLMError):
    """The provider cannot serve the call: an outage, a network failure, or
    a rejected key or model, which needs a developer rather than a retry."""

    message_key = N_(
        "The language model is unavailable right now. Please try again later."
    )


class LLMRefused(LLMError):
    """The model declined the request on policy grounds; trying the same
    request again gives the same answer."""

    message_key = N_("The language model declined this request.")
    log_level = "info"
    capture_to_sentry = False


class LLMOutputError(LLMError):
    """A structured answer failed validation even after the repair attempt, or
    was cut at max_tokens. The provider billed the answers all the same, so
    `usage` is what they cost."""

    message_key = N_(
        "The language model gave an answer the bot could not read. Please try again."
    )
