from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from src.core.llm.enums import LLMProviderName

# The width of llm_usage.purpose.
PURPOSE_MAX_LENGTH = 64


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """One call to a model.

    `purpose` names the caller in llm_usage and in the call's log line: a
    domain constant such as NOTE_SUMMARY_PURPOSE, never text a user typed.
    `model` None takes the client's default model. `temperature` None sends
    none; claude-sonnet-5-5 and claude-haiku-5-5 reject any value but their
    default. `cache_system` asks the provider to cache the system prompt,
    which pays off only for a long prompt reused within minutes.
    """

    purpose: str
    messages: Sequence[LLMMessage]
    system: str | None = None
    model: str | None = None
    max_tokens: int = 1024
    temperature: float | None = None
    cache_system: bool = False

    def __post_init__(self) -> None:
        # Checked here rather than by the provider: a malformed request is a bug
        # in the caller and must not be reported, retried or failed over as an
        # outage.
        if not self.purpose:
            raise ValueError("purpose must name the caller")
        # A longer one would fit no llm_usage row, and the recorder's failed
        # write is only logged: the caller's whole spend would go missing.
        if len(self.purpose) > PURPOSE_MAX_LENGTH:
            raise ValueError(f"purpose is longer than {PURPOSE_MAX_LENGTH} characters")
        if not self.messages:
            raise ValueError("a request needs at least one message")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token counts of one call and what they cost.

    `input_tokens` are the ones billed at the full input rate. Cache reads
    and cache writes are counted apart, as Anthropic reports them; an adapter
    for an API that folds cached tokens into the prompt count subtracts them.
    `cost_usd` is None when the model's price is unknown.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """`model` and `provider` name what actually answered, which after a
    fallback differs from what the request asked for."""

    text: str
    usage: LLMUsage
    model: str
    provider: LLMProviderName
    stop_reason: str | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class StructuredResponse[T: BaseModel]:
    """`output` is already validated against the requested model; `response`
    keeps the raw text, usage and provider of the call that produced it."""

    output: T
    response: LLMResponse
