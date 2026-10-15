import pytest

from src.core.errors.exceptions import CoreException
from src.core.llm.enums import LLMProviderName
from src.core.llm.errors import (
    LLMError,
    LLMNotConfigured,
    LLMOutputError,
    LLMRateLimited,
    LLMRefused,
    LLMTimeout,
    LLMUnavailable,
)
from src.core.llm.types import LLMUsage

ALL_LLM_ERRORS = [
    LLMError,
    LLMNotConfigured,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
    LLMRefused,
    LLMOutputError,
]


@pytest.mark.parametrize("error_class", ALL_LLM_ERRORS, ids=lambda c: c.__name__)
def test_every_llm_error_carries_the_failing_provider(
    error_class: type[LLMError],
) -> None:
    """The fallback decides and the log line reports by provider; an error
    that lost it could not say which vendor is down."""
    error = error_class("HTTP 529", provider=LLMProviderName.ANTHROPIC)

    assert isinstance(error, CoreException)
    assert error.provider is LLMProviderName.ANTHROPIC
    assert error.detail == "HTTP 529"


@pytest.mark.parametrize("error_class", ALL_LLM_ERRORS, ids=lambda c: c.__name__)
def test_an_error_raised_before_any_provider_has_none(
    error_class: type[LLMError],
) -> None:
    assert error_class().provider is None


@pytest.mark.parametrize(
    ("error_class", "captured"),
    [
        (LLMError, True),
        (LLMUnavailable, True),
        (LLMOutputError, True),
        (LLMNotConfigured, False),
        (LLMRateLimited, False),
        (LLMTimeout, False),
        (LLMRefused, False),
    ],
    ids=lambda value: value.__name__ if isinstance(value, type) else str(value),
)
def test_only_failures_a_developer_must_fix_reach_sentry(
    error_class: type[LLMError], captured: bool
) -> None:
    """A rejected key, an outage or a schema the model cannot meet needs a
    developer; a busy provider, a slow answer or a refusal passes on its own
    and would only flood Sentry."""
    assert error_class.capture_to_sentry is captured


def test_each_llm_failure_tells_the_user_something_different() -> None:
    """The user decides whether to retry now, later or never from this text,
    so two kinds of failure must not read the same."""
    keys = [error_class.message_key for error_class in ALL_LLM_ERRORS]

    assert len(set(keys)) == len(keys)


@pytest.mark.parametrize("error_class", ALL_LLM_ERRORS, ids=lambda c: c.__name__)
def test_every_llm_error_can_carry_what_was_billed(
    error_class: type[LLMError],
) -> None:
    """A refusal, a failed repair or a repair request cut by the network can
    each come after an answer the provider billed; llm_usage records the
    failure with that cost only if the error brings it along."""
    usage = LLMUsage(input_tokens=10, output_tokens=5)

    assert error_class("failed", usage=usage).usage is usage
    assert error_class().usage is None
