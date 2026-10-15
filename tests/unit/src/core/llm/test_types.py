from decimal import Decimal

from pydantic import BaseModel
import pytest

from src.core.llm.enums import LLMProviderName, ModelTier
from src.core.llm.types import (
    MAX_IMAGE_BYTES,
    PURPOSE_MAX_LENGTH,
    ImageBlock,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMTool,
    LLMUsage,
    ProviderBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

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


class Lookup(BaseModel):
    query: str


PNG = ImageBlock(media_type="image/png", data=b"\x89PNG")
CALL = ToolUseBlock(id="call_1", name="lookup", input={"query": "rice"})
RESULT = ToolResultBlock(tool_use_id="call_1", content="found")
THINKING = ProviderBlock(provider=LLMProviderName.ANTHROPIC, data={"type": "thinking"})


@pytest.mark.parametrize("block", [PNG, RESULT])
def test_what_only_the_user_sends_is_refused_in_an_assistant_turn(
    block: ImageBlock | ToolResultBlock,
) -> None:
    """Both APIs answer 400 to an image or a tool result from the assistant;
    the caller's bug must not be retried or failed over as an outage."""
    with pytest.raises(ValueError, match="assistant"):
        LLMMessage(role="assistant", content=[block])


@pytest.mark.parametrize("block", [CALL, THINKING])
def test_what_only_the_model_produces_is_refused_in_a_user_turn(
    block: ToolUseBlock | ProviderBlock,
) -> None:
    with pytest.raises(ValueError, match="user"):
        LLMMessage(role="user", content=[block])


def test_a_message_of_no_blocks_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one block"):
        LLMMessage(role="user", content=[])


def test_a_string_message_stays_a_string() -> None:
    """Existing callers send strings, and their request bodies must not change."""
    assert LLMMessage(role="user", content="hello").content == "hello"


def test_an_image_type_the_providers_do_not_take_is_refused() -> None:
    with pytest.raises(ValueError, match="image/bmp"):
        ImageBlock(media_type="image/bmp", data=b"BM")


def test_an_image_over_the_provider_limit_is_refused() -> None:
    """Anthropic refuses an image over 5 MB with a 400, which would read as an
    outage and switch to the fallback for nothing."""
    with pytest.raises(ValueError, match="5242880"):
        ImageBlock(media_type="image/jpeg", data=b"x" * (MAX_IMAGE_BYTES + 1))


def test_an_empty_image_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        ImageBlock(media_type="image/jpeg", data=b"")


@pytest.mark.parametrize("name", ["", "has space", "x" * 65, "точка"])
def test_a_tool_name_both_apis_refuse_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="tool name"):
        LLMTool(name=name, description="d", args=Lookup)


def test_two_tools_of_one_name_are_refused() -> None:
    tool = LLMTool(name="lookup", description="d", args=Lookup)
    with pytest.raises(ValueError, match="lookup"):
        LLMRequest(purpose="test", messages=HELLO, tools=(tool, tool))


def test_a_request_defaults_to_the_strong_tier_without_tools() -> None:
    """The defaults keep every existing call as it was."""
    request = LLMRequest(purpose="test", messages=HELLO)
    assert request.tier is ModelTier.STRONG
    assert request.tools == ()
    assert request.tool_choice == "auto"
    assert request.effort is None


def _response(*content: TextBlock | ToolUseBlock | ProviderBlock) -> LLMResponse:
    return LLMResponse(
        content=content,
        usage=LLMUsage(input_tokens=1, output_tokens=1),
        model="claude-sonnet-5-5",
        provider=LLMProviderName.ANTHROPIC,
        stop="tool_use",
        stop_reason="tool_use",
        latency_ms=1,
    )


def test_a_response_reads_as_its_text_and_its_calls() -> None:
    response = _response(THINKING, TextBlock("Let me look. "), CALL, TextBlock("Done."))
    assert response.text == "Let me look. Done."
    assert response.tool_calls == (CALL,)


def test_the_assistant_turn_keeps_every_block_in_order() -> None:
    """Anthropic refuses a tool loop whose assistant turn lost its signed
    thinking block, so the turn goes back exactly as it came."""
    response = _response(THINKING, CALL)
    assert response.assistant_turn == LLMMessage(
        role="assistant", content=(THINKING, CALL)
    )


def test_an_empty_answer_still_makes_an_assistant_turn() -> None:
    assert _response().assistant_turn == LLMMessage(role="assistant", content="")


def test_the_text_of_a_message_is_its_text_blocks_only() -> None:
    """Adapters and the transcript without block support read this; an image
    or a call must not leak into it as text."""
    assert LLMMessage(role="user", content="hello").text == "hello"
    mixed = LLMMessage(
        role="user", content=(TextBlock("a"), PNG, TextBlock("b"), RESULT)
    )
    assert mixed.text == "ab"
    assert LLMMessage(role="user", content=(PNG,)).text == ""
