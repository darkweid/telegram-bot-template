import base64
from collections.abc import Mapping
import dataclasses
from functools import partial
import time
from typing import Literal, cast

import anthropic
from anthropic import AsyncAnthropic, Omit, omit, transform_schema
from anthropic.types import (
    Base64ImageSourceParam,
    CacheControlEphemeralParam,
    ContentBlock as AnthropicContentBlock,
    ContentBlockParam,
    ImageBlockParam,
    JSONOutputFormatParam,
    Message,
    MessageParam,
    OutputConfigParam,
    RedactedThinkingBlock as AnthropicRedactedThinkingBlock,
    TextBlock as AnthropicTextBlock,
    TextBlockParam,
    ThinkingBlock as AnthropicThinkingBlock,
    ToolChoiceAutoParam,
    ToolChoiceNoneParam,
    ToolChoiceParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlock as AnthropicToolUseBlock,
    ToolUseBlockParam,
)
import httpx2
from pydantic import BaseModel, JsonValue

from src.core.llm.enums import LLMProviderName, ModelTier
from src.core.llm.errors import (
    LLMError,
    LLMOutputError,
    LLMRateLimited,
    LLMRefused,
    LLMTimeout,
    LLMUnavailable,
)
from src.core.llm.pricing import PricingTable
from src.core.llm.reporting import report_llm_call
from src.core.llm.structured import validated_with_repair
from src.core.llm.types import (
    AnswerBlock,
    ContentBlock,
    ImageBlock,
    LLMRequest,
    LLMResponse,
    LLMStop,
    LLMUsage,
    ProviderBlock,
    StructuredResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"

_PROVIDER = LLMProviderName.ANTHROPIC

_STOPS: Mapping[str, LLMStop] = {
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
}


def _stop(reason: str | None) -> LLMStop:
    # end_turn, stop_sequence, pause_turn: the turn is over either way.
    return _STOPS.get(reason or "", "end")


class AnthropicLLMClient:
    """LLMClient over the Anthropic Messages API.

    Retries (408, 409, 429, 5xx, connection errors) and the per-attempt
    timeout are the SDK's own, set from LLM_MAX_RETRIES and
    LLM_TIMEOUT_SECONDS. The cost is computed from the response's usage with
    the price table: the API does not return one.

    The model is the request's own or the configured one of its tier, and the
    call is priced by that id.

    `structured` asks for JSON through `output_config.format`, not forced
    tool use: claude-sonnet-5-5 and claude-opus-5-5 reject `tool_choice`
    "tool" and "any" with a 400. `LLMRequest.temperature` is not sent: the current
    models reject sampling parameters and the SDK no longer accepts them.

    Tools go out strict with `disable_parallel_tool_use`; `tool_choice` "any"
    and "tool" are not offered, for the same 400. Thinking is not requested:
    the current models think on their own, and their signed blocks come back
    as `ProviderBlock`s.
    """

    def __init__(
        self,
        *,
        api_key: str,
        models: Mapping[ModelTier, str],
        pricing: PricingTable,
        timeout_seconds: float,
        max_retries: int,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        # base_url is explicit so ANTHROPIC_BASE_URL in the environment cannot
        # send the key somewhere else.
        self._client = AsyncAnthropic(
            api_key=api_key,
            base_url=ANTHROPIC_BASE_URL,
            timeout=timeout_seconds,
            max_retries=max_retries,
            http_client=http_client,
        )
        self._models = models
        self._pricing = pricing

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self._models[request.tier]
        started = time.perf_counter()
        try:
            response = await self._send(request, model=model, output_format=None)
        except LLMError as error:
            self._report(request, model, started, None, error)
            raise
        self._report(request, model, started, response, None)
        return response

    async def structured[T: BaseModel](
        self, request: LLMRequest, output: type[T]
    ) -> StructuredResponse[T]:
        model = request.model or self._models[request.tier]
        # transform_schema makes the pydantic schema strict (no extra keys)
        # and drops constraints the API cannot enforce; pydantic still checks
        # them on the answer.
        output_format: JSONOutputFormatParam = {
            "type": "json_schema",
            "schema": transform_schema(output),
        }
        send = partial(self._send, model=model, output_format=output_format)
        started = time.perf_counter()
        try:
            result = await validated_with_repair(send, request, output, _PROVIDER)
        except LLMError as error:
            self._report(request, model, started, None, error)
            raise
        self._report(request, model, started, result.response, None)
        return result

    async def _send(
        self,
        request: LLMRequest,
        *,
        model: str,
        output_format: JSONOutputFormatParam | None,
    ) -> LLMResponse:
        started = time.perf_counter()
        try:
            message = await self._client.messages.create(
                model=model,
                max_tokens=request.max_tokens,
                system=_system(request),
                messages=_messages(request),
                tools=_tools(request),
                tool_choice=_tool_choice(request),
                output_config=_output_config(request, output_format),
                # With the system breakpoint, a second one at the end of the
                # history: a tool loop resends all of it every step.
                cache_control=(
                    CacheControlEphemeralParam(type="ephemeral")
                    if request.cache_system
                    else omit
                ),
            )
        except anthropic.APITimeoutError:
            raise LLMTimeout(
                "Anthropic request timed out", provider=_PROVIDER
            ) from None
        except anthropic.RateLimitError as error:
            raise LLMRateLimited(_describe(error), provider=_PROVIDER) from None
        except anthropic.AnthropicError as error:
            # Chained from None: an SDK error's message carries the response
            # body, which can echo the request.
            raise LLMUnavailable(_describe(error), provider=_PROVIDER) from None
        except ValueError:
            # A 200 whose body is cut or empty: the JSON decoder's error holds
            # the whole body, which can quote the answer, so nothing is kept.
            raise LLMUnavailable(
                "Anthropic returned an unreadable body", provider=_PROVIDER
            ) from None
        if not isinstance(message, Message):
            # The SDK hands back a non-JSON 200 body (an HTML error page from
            # a proxy) as a plain string.
            raise LLMUnavailable(
                "Anthropic returned a body that is not JSON", provider=_PROVIDER
            ) from None
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            return self._response(
                message,
                model=model,
                structured=output_format is not None,
                latency_ms=latency_ms,
            )
        except (AttributeError, TypeError):
            # The SDK builds the message without validating it, so a JSON body
            # that is no message (an error object, a null usage or content)
            # fails on first access to the missing field.
            raise LLMUnavailable(
                "Anthropic returned a body that is not a message", provider=_PROVIDER
            ) from None

    def _response(
        self, message: Message, *, model: str, structured: bool, latency_ms: int
    ) -> LLMResponse:
        tokens = LLMUsage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_tokens=message.usage.cache_read_input_tokens or 0,
            cache_write_tokens=message.usage.cache_creation_input_tokens or 0,
        )
        # Priced by the requested id: the price table and the config use it.
        usage = dataclasses.replace(tokens, cost_usd=self._pricing.cost(model, tokens))
        if message.stop_reason == "refusal":
            category = (
                None if message.stop_details is None else message.stop_details.category
            )
            # A refusal is billed like any answer.
            raise LLMRefused(
                f"Anthropic refusal category={category}",
                provider=_PROVIDER,
                usage=usage,
            )
        stop = _stop(message.stop_reason)
        content = [
            block
            for block in (_answer_block(item) for item in message.content)
            if block is not None
        ]
        cut_call = stop == "max_tokens" and any(
            isinstance(block, ToolUseBlock) for block in content
        )
        if stop == "max_tokens" and (structured or cut_call):
            # Truncated JSON fails validation again on a repair attempt that
            # has even less room left, and a cut tool call carries half its
            # arguments, so neither is attempted. The answer was billed.
            raise LLMOutputError(
                (
                    "tool call cut at max_tokens"
                    if cut_call
                    else "structured output cut at max_tokens"
                ),
                provider=_PROVIDER,
                usage=usage,
            )
        return LLMResponse(
            content=content,
            usage=usage,
            model=message.model,
            provider=_PROVIDER,
            stop=stop,
            stop_reason=message.stop_reason,
            latency_ms=latency_ms,
        )

    def _report(
        self,
        request: LLMRequest,
        model: str,
        started: float,
        response: LLMResponse | None,
        error: LLMError | None,
    ) -> None:
        report_llm_call(
            provider=_PROVIDER,
            model=model if response is None else response.model,
            request=request,
            response=response,
            error=error,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _system(request: LLMRequest) -> str | list[TextBlockParam] | Omit:
    if request.system is None:
        return omit
    if not request.cache_system:
        return request.system
    # The breakpoint caches everything up to the end of the system prompt.
    # A prompt below the model's minimum cacheable length is sent uncached
    # without an error.
    return [
        TextBlockParam(
            type="text", text=request.system, cache_control={"type": "ephemeral"}
        )
    ]


def _describe(error: anthropic.AnthropicError) -> str:
    if isinstance(error, anthropic.APIStatusError):
        return (
            f"{type(error).__name__} status={error.status_code} "
            f"request_id={error.request_id}"
        )
    return type(error).__name__


def _messages(request: LLMRequest) -> list[MessageParam]:
    rendered: list[MessageParam] = []
    for message in request.messages:
        if isinstance(message.content, str):
            rendered.append(MessageParam(role=message.role, content=message.content))
            continue
        blocks = [
            block
            for block in (_block(item) for item in message.content)
            if block is not None
        ]
        # A turn of the other provider's reasoning alone is left with nothing,
        # and the API refuses an empty one.
        if blocks:
            rendered.append(MessageParam(role=message.role, content=blocks))
    return rendered


def _block(block: ContentBlock) -> ContentBlockParam | None:
    match block:
        case TextBlock(text=text):
            return TextBlockParam(type="text", text=text)
        case ImageBlock(media_type=media_type, data=data):
            return ImageBlockParam(
                type="image",
                source=Base64ImageSourceParam(
                    type="base64",
                    # ImageBlock admits only the four types the API names.
                    media_type=cast(
                        Literal["image/jpeg", "image/png", "image/gif", "image/webp"],
                        media_type,
                    ),
                    data=base64.b64encode(data).decode("ascii"),
                ),
            )
        case ToolUseBlock(id=call_id, name=name, input=arguments):
            return ToolUseBlockParam(
                type="tool_use",
                id=call_id,
                name=name,
                # The param type is invariant in dict; the values are JSON.
                input=cast(dict[str, object], arguments),
            )
        case ToolResultBlock(tool_use_id=call_id, content=content, is_error=is_error):
            return ToolResultBlockParam(
                type="tool_result",
                tool_use_id=call_id,
                content=content,
                is_error=is_error,
            )
        case ProviderBlock(provider=provider, data=data):
            # Thinking is signed by the API and accepted back only unchanged;
            # the other provider's reasoning means nothing here.
            return cast(ContentBlockParam, data) if provider is _PROVIDER else None


def _answer_block(block: AnthropicContentBlock) -> AnswerBlock | None:
    match block:
        case AnthropicTextBlock():
            return TextBlock(block.text)
        case AnthropicToolUseBlock():
            if not isinstance(block.input, dict):
                # Caught with the other malformed bodies in _send.
                raise TypeError("tool_use input is not an object")
            return ToolUseBlock(
                id=block.id,
                name=block.name,
                input=cast(dict[str, JsonValue], block.input),
            )
        case AnthropicThinkingBlock() | AnthropicRedactedThinkingBlock():
            return ProviderBlock(
                provider=_PROVIDER,
                data=cast(JsonValue, block.model_dump(mode="json", exclude_none=True)),
            )
        case _:
            # Server tools and other kinds this client never asks for.
            return None


def _tools(request: LLMRequest) -> list[ToolParam] | Omit:
    if not request.tools:
        return omit
    return [
        ToolParam(
            name=tool.name,
            description=tool.description,
            input_schema=transform_schema(tool.args),
            strict=True,
        )
        for tool in request.tools
    ]


def _tool_choice(request: LLMRequest) -> ToolChoiceParam | Omit:
    if not request.tools:
        return omit
    if request.tool_choice == "none":
        return ToolChoiceNoneParam(type="none")
    # "any" and "tool" are not offered: claude-sonnet-5-5 answers them with a
    # 400. One call per answer keeps the loop's steps one decision each.
    return ToolChoiceAutoParam(type="auto", disable_parallel_tool_use=True)


def _output_config(
    request: LLMRequest, output_format: JSONOutputFormatParam | None
) -> OutputConfigParam | Omit:
    config: OutputConfigParam = {}
    if request.effort is not None:
        config["effort"] = request.effort
    if output_format is not None:
        config["format"] = output_format
    return config or omit
