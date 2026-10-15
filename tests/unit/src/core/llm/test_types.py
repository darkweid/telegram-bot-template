from decimal import Decimal

import pytest

from src.core.llm.types import PURPOSE_MAX_LENGTH, LLMMessage, LLMRequest, LLMUsage

HELLO = (LLMMessage(role="user", content="hello"),)


def test_a_request_without_messages_is_refused() -> None:
    """Providers reject an empty conversation; failing in the caller keeps a
    bug from being reported, retried or failed over as an outage."""
    with pytest.raises(ValueError, match="at least one message"):
        LLMRequest(purpose="test", messages=())


def test_a_request_must_name_its_purpose() -> None:
    """llm_usage and /stats group spending by purpose; an anonymous call
    would be money nobody can attribute."""
    with pytest.raises(ValueError, match="purpose"):
        LLMRequest(purpose="", messages=HELLO)


@pytest.mark.parametrize("max_tokens", [0, -1])
def test_max_tokens_must_be_positive(max_tokens: int) -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        LLMRequest(purpose="test", messages=HELLO, max_tokens=max_tokens)


def test_a_request_leaves_model_and_sampling_to_the_client() -> None:
    """None means "the client's default": a domain call stays valid when
    the deployment switches models, and no sampling value is sent to models
    that reject one."""
    request = LLMRequest(purpose="test", messages=HELLO)

    assert request.model is None
    assert request.temperature is None
    assert request.system is None
    assert request.cache_system is False
    assert request.max_tokens == 1024


def test_usage_without_cache_or_price_is_explicit() -> None:
    """An unknown price is None, never zero: a zero would read as a free call
    in /stats."""
    usage = LLMUsage(input_tokens=10, output_tokens=5)

    assert usage.cache_read_tokens == 0
    assert usage.cache_write_tokens == 0
    assert usage.cost_usd is None
    assert LLMUsage(1, 1, cost_usd=Decimal("0.1")).cost_usd == Decimal("0.1")


def test_a_purpose_wider_than_its_column_is_refused() -> None:
    """A purpose llm_usage cannot store would lose every row of that caller,
    and the failed write is only logged."""
    LLMRequest(purpose="p" * PURPOSE_MAX_LENGTH, messages=HELLO)
    with pytest.raises(ValueError, match="purpose"):
        LLMRequest(purpose="p" * (PURPOSE_MAX_LENGTH + 1), messages=HELLO)
