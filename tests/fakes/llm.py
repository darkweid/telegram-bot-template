"""An LLMClient that answers from a queue, as FakeTelegramSession does for the
Bot API."""

from collections import deque
from collections.abc import Sequence
from decimal import Decimal
import hashlib
import json

from pydantic import BaseModel, JsonValue

from src.core.llm.enums import LLMProviderName
from src.core.llm.structured import refuse_tools
from src.core.llm.types import (
    AnswerBlock,
    LLMRequest,
    LLMResponse,
    LLMStop,
    LLMUsage,
    StructuredResponse,
    TextBlock,
    ToolUseBlock,
)


def llm_response(
    text: str = "ok",
    *,
    content: Sequence[AnswerBlock] | None = None,
    stop: LLMStop = "end",
    model: str = "claude-sonnet-5-5",
    provider: LLMProviderName = LLMProviderName.ANTHROPIC,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cost_usd: Decimal | None = Decimal("0.00010000"),
    stop_reason: str | None = "end_turn",
    latency_ms: int = 100,
) -> LLMResponse:
    """`content` replaces the one text block `text` makes."""
    return LLMResponse(
        content=(TextBlock(text),) if content is None else tuple(content),
        usage=LLMUsage(
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd
        ),
        model=model,
        provider=provider,
        stop=stop,
        stop_reason=stop_reason,
        latency_ms=latency_ms,
    )


def tool_call(
    name: str, input: dict[str, JsonValue], *, id: str | None = None
) -> ToolUseBlock:
    """The default id is derived from the name and the input, so it does not
    depend on test order; two identical calls in one answer need explicit ids."""
    if id is None:
        digest = hashlib.sha256(json.dumps([name, input], sort_keys=True).encode())
        id = f"call_{digest.hexdigest()[:12]}"
    return ToolUseBlock(id=id, name=name, input=input)


type _Queued = LLMResponse | StructuredResponse[BaseModel] | BaseException


class FakeLLMClient:
    """Each call takes the next queued item: a response, a structured output,
    or an exception to raise. `complete` with nothing queued answers
    `llm_response()`; `structured` with nothing queued fails the test, since
    it cannot invent the caller's model."""

    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []
        self._queued: deque[_Queued] = deque()

    def queue(self, result: LLMResponse | BaseException) -> None:
        self._queued.append(result)

    def queue_structured(
        self, output: BaseModel, response: LLMResponse | None = None
    ) -> None:
        self._queued.append(
            StructuredResponse(
                output=output,
                response=response or llm_response(output.model_dump_json()),
            )
        )

    def queue_tool_calls(self, *calls: ToolUseBlock, text: str = "") -> None:
        """An answer that asks for `calls`, after `text` when there is any."""
        lead: tuple[AnswerBlock, ...] = (TextBlock(text),) if text else ()
        self._queued.append(
            llm_response(
                content=(*lead, *calls), stop="tool_use", stop_reason="tool_use"
            )
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if not self._queued:
            return llm_response()
        item = self._queued.popleft()
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, StructuredResponse):
            raise AssertionError("a structured output is queued, complete() was called")
        return item

    async def structured[T: BaseModel](
        self, request: LLMRequest, output: type[T]
    ) -> StructuredResponse[T]:
        refuse_tools(request)
        self.calls.append(request)
        if not self._queued:
            raise AssertionError(f"structured({output.__name__}) with nothing queued")
        item = self._queued.popleft()
        if isinstance(item, BaseException):
            raise item
        if not isinstance(item, StructuredResponse) or not isinstance(
            item.output, output
        ):
            raise AssertionError(f"structured({output.__name__}) found {item!r}")
        return StructuredResponse(output=item.output, response=item.response)
