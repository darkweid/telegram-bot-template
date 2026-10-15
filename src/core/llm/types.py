from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Literal

from pydantic import BaseModel, JsonValue

from src.core.llm.enums import LLMProviderName, ModelTier

# The width of llm_usage.purpose.
PURPOSE_MAX_LENGTH = 64
# What both providers take as an image. Anthropic's 5 MB per-image limit
# applies to the base64 payload, which is 4/3 of the bytes, so the raw limit
# is three quarters of it.
IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
MAX_IMAGE_BYTES = 5 * 1024 * 1024 * 3 // 4
# The narrower of the two APIs' rules for a tool name.
_TOOL_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}")

type LLMStop = Literal["end", "tool_use", "max_tokens"]


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ImageBlock:
    """Checked here: a provider answers a bad image with a 400, which reads as
    an outage and would switch to the fallback for a bug in the caller."""

    media_type: str
    data: bytes

    def __post_init__(self) -> None:
        if self.media_type not in IMAGE_MEDIA_TYPES:
            raise ValueError(
                f"image media type {self.media_type} is not one of "
                f"{sorted(IMAGE_MEDIA_TYPES)}"
            )
        if not self.data:
            raise ValueError("an image cannot be empty")
        if len(self.data) > MAX_IMAGE_BYTES:
            raise ValueError(f"an image may not exceed {MAX_IMAGE_BYTES} bytes")


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """A call the model asked for. `input` is the model's JSON as it came:
    validating it against the tool's arguments is the caller's job, so that a
    bad call can go back to the model as an error instead of failing the run."""

    id: str
    name: str
    input: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ProviderBlock:
    """Part of an answer only its provider can read back: Anthropic's signed
    thinking, OpenRouter's reasoning_details. It goes back into the history
    unchanged; the other provider's adapter drops it."""

    provider: LLMProviderName
    data: JsonValue


type ContentBlock = (
    TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock | ProviderBlock
)
type AnswerBlock = TextBlock | ToolUseBlock | ProviderBlock

_USER_ONLY = (ImageBlock, ToolResultBlock)
_ASSISTANT_ONLY = (ToolUseBlock, ProviderBlock)


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """A string is sent as a string, so text-only requests keep their bodies;
    only a sequence is sent as blocks."""

    role: Literal["user", "assistant"]
    content: str | Sequence[ContentBlock]

    def __post_init__(self) -> None:
        if isinstance(self.content, str):
            return
        if not self.content:
            raise ValueError("a message needs at least one block")
        misplaced = _ASSISTANT_ONLY if self.role == "user" else _USER_ONLY
        for block in self.content:
            if isinstance(block, misplaced):
                raise ValueError(
                    f"{type(block).__name__} cannot be part of a {self.role} message"
                )


@dataclass(frozen=True, slots=True)
class LLMTool:
    """A tool the model may call. Every field of `args` is required in the
    schema both APIs get (an optional one is `T | None` with no default), and
    length limits are field validators: strict schemas refuse maxLength."""

    name: str
    description: str
    args: type[BaseModel]

    def __post_init__(self) -> None:
        if not _TOOL_NAME.fullmatch(self.name):
            raise ValueError(f"tool name {self.name!r} must match {_TOOL_NAME.pattern}")


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """One call to a model.

    `purpose` names the caller in llm_usage and in the call's log line: a
    domain constant such as NOTE_SUMMARY_PURPOSE, never text a user typed.
    `model` None takes the configured model of `tier`; a model named here is
    the primary vendor's id and is dropped on a fallback. `temperature` None
    sends none; claude-sonnet-5-5 and claude-haiku-5-5 reject any value but
    their default. `cache_system` asks the provider to cache the system prompt
    (and, on Anthropic, the history), which pays off only for a long prompt
    reused within minutes. `effort` None leaves the model's own default.
    A model may answer with several tool calls at once.
    """

    purpose: str
    messages: Sequence[LLMMessage]
    system: str | None = None
    model: str | None = None
    max_tokens: int = 1024
    temperature: float | None = None
    cache_system: bool = False
    tools: Sequence[LLMTool] = ()
    tool_choice: Literal["auto", "none"] = "auto"
    tier: ModelTier = ModelTier.STRONG
    effort: Literal["low", "medium", "high"] | None = None

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
        names = [tool.name for tool in self.tools]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"tools share a name: {', '.join(duplicates)}")


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

    def __post_init__(self) -> None:
        # The SDKs build responses without validating them, so a malformed body
        # can hand over a null count. Refused here, inside the adapter's guard
        # for such bodies, rather than in the log line or the llm_usage insert.
        counts = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
        )
        if not all(isinstance(count, int) for count in counts):
            raise TypeError("token counts must be integers")


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """`model` and `provider` name what actually answered, which after a
    fallback differs from what the request asked for. `stop` is the adapter's
    reading of the provider's reason, kept raw in `stop_reason`."""

    content: Sequence[AnswerBlock]
    usage: LLMUsage
    model: str
    provider: LLMProviderName
    stop: LLMStop
    stop_reason: str | None
    latency_ms: int

    @property
    def text(self) -> str:
        return "".join(
            block.text for block in self.content if isinstance(block, TextBlock)
        )

    @property
    def tool_calls(self) -> tuple[ToolUseBlock, ...]:
        return tuple(block for block in self.content if isinstance(block, ToolUseBlock))

    @property
    def assistant_turn(self) -> LLMMessage:
        """The answer as the next request's history needs it, provider blocks
        included. An empty answer becomes an empty string: a message of no
        blocks is refused."""
        if not self.content:
            return LLMMessage(role="assistant", content="")
        return LLMMessage(role="assistant", content=tuple(self.content))


@dataclass(frozen=True, slots=True)
class StructuredResponse[T: BaseModel]:
    """`output` is already validated against the requested model; `response`
    keeps the raw text, usage and provider of the call that produced it."""

    output: T
    response: LLMResponse
