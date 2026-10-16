import base64
from collections.abc import Awaitable, Mapping, Sequence
from decimal import Decimal
from functools import partial
import json
import time
from typing import cast

import httpx2
import openai
from openai import AsyncOpenAI, Omit, omit, pydantic_function_tool
from openai.lib import type_to_response_format_param
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionAssistantMessageParam,
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCall,
    ChatCompletionMessageToolCallUnion,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolUnionParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion import Choice
from openai.types.chat.completion_create_params import ResponseFormat
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
from src.core.llm.reporting import billed_usage, report_llm_call
from src.core.llm.structured import validated_with_repair
from src.core.llm.types import (
    AnswerBlock,
    ContentBlock,
    ImageBlock,
    LLMMessage,
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

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_PROVIDER = LLMProviderName.OPENROUTER

# The SDK builds a completion without validating it, so a JSON body that is
# no completion (a choice without a message, a null usage count) fails with
# AttributeError, TypeError or ValueError on first access to the missing field.
MALFORMED_COMPLETION = "OpenRouter returned a body that is not a chat completion"


class OpenRouterLLMClient:
    """LLMClient over OpenRouter's OpenAI-compatible chat completions API.

    Retries (429, 5xx, connection errors) and the per-attempt timeout are the
    SDK's own, set from LLM_MAX_RETRIES and LLM_TIMEOUT_SECONDS. `cache_system`
    marks the system prompt for `anthropic/*` models only: the other vendors
    cache repeated prompt prefixes on their own. `parallel_tool_calls` is never
    sent: OpenRouter does not know it for Anthropic models, so an answer may
    carry several calls. The cost comes from OpenRouter's
    `usage.cost`, so no price table is needed here.

    The model is the request's own or the configured one of its tier.
    """

    def __init__(
        self,
        *,
        api_key: str,
        models: Mapping[ModelTier, str],
        app_url: str,
        app_title: str,
        timeout_seconds: float,
        max_retries: int,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._client = openrouter_client(
            api_key=api_key,
            app_url=app_url,
            app_title=app_title,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            http_client=http_client,
        )
        self._models = models

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self._models[request.tier]
        started = time.perf_counter()
        try:
            response = await self._send(request, model=model, response_format=omit)
        except LLMError as error:
            self._report(request, model, started, None, error)
            raise
        self._report(request, model, started, response, None)
        return response

    async def structured[T: BaseModel](
        self, request: LLMRequest, output: type[T]
    ) -> StructuredResponse[T]:
        model = request.model or self._models[request.tier]
        send = partial(
            self._send,
            model=model,
            response_format=type_to_response_format_param(output),
        )
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
        response_format: ResponseFormat | Omit,
    ) -> LLMResponse:
        structured = not isinstance(response_format, Omit)
        started = time.perf_counter()
        completion = await checked_completion(
            self._client.chat.completions.create(
                model=model,
                messages=_messages(request, model),
                max_tokens=request.max_tokens,
                temperature=(
                    omit if request.temperature is None else request.temperature
                ),
                response_format=response_format,
                tools=_tools(request),
                tool_choice=_tool_choice(request),
                extra_body=_extra_body(request, structured=structured),
            )
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            return _response(completion, structured=structured, latency_ms=latency_ms)
        except (AttributeError, TypeError, ValueError):
            raise LLMUnavailable(MALFORMED_COMPLETION, provider=_PROVIDER) from None

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
            purpose=request.purpose,
            usage=billed_usage(None if response is None else response.usage, error),
            error=error,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def openrouter_client(
    *,
    api_key: str,
    app_url: str,
    app_title: str,
    timeout_seconds: float,
    max_retries: int,
    http_client: httpx2.AsyncClient | None = None,
) -> AsyncOpenAI:
    """Retries (429, 5xx, connection errors) and the per-attempt timeout are
    the SDK's own. The caller closes the client."""
    # OpenRouter attributes traffic to an app by these two headers; an empty
    # value is left out rather than sent blank.
    attribution = {
        name: value
        for name, value in (("HTTP-Referer", app_url), ("X-Title", app_title))
        if value
    }
    # base_url is explicit so OPENAI_BASE_URL in the environment cannot send
    # the key somewhere else.
    return AsyncOpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        timeout=timeout_seconds,
        max_retries=max_retries,
        default_headers=attribution,
        http_client=http_client,
    )


async def checked_completion(call: Awaitable[object]) -> ChatCompletion:
    """Await an SDK chat completion call and map every SDK failure and every
    body that is not JSON to an LLMError, chained from None: the SDK's
    message carries the response body, which can echo the request.

    The completion itself is not validated: reading it may still raise
    AttributeError, TypeError or ValueError (`MALFORMED_COMPLETION`).
    """
    try:
        completion = await call
    except openai.APITimeoutError:
        raise LLMTimeout("OpenRouter request timed out", provider=_PROVIDER) from None
    except openai.RateLimitError as error:
        raise LLMRateLimited(_describe(error), provider=_PROVIDER) from None
    except openai.OpenAIError as error:
        raise LLMUnavailable(_describe(error), provider=_PROVIDER) from None
    except ValueError:
        # A 200 whose body is cut or empty: the JSON decoder's error holds
        # the whole body, which can quote the answer, so nothing is kept.
        raise LLMUnavailable(
            "OpenRouter returned an unreadable body", provider=_PROVIDER
        ) from None
    if not isinstance(completion, ChatCompletion):
        # The SDK hands back a non-JSON 200 body (an HTML error page from a
        # proxy) as a plain string.
        raise LLMUnavailable(
            "OpenRouter returned a body that is not JSON", provider=_PROVIDER
        ) from None
    return completion


def first_choice(completion: ChatCompletion) -> Choice:
    """The answer's choice, once OpenRouter reported no failure and no
    refusal. A refusal is billed: LLMRefused carries the completion's usage.
    """
    if not completion.choices:
        raise LLMUnavailable("OpenRouter returned no choices", provider=_PROVIDER)
    choice = completion.choices[0]
    finish_reason: str | None = choice.finish_reason
    if finish_reason == "error":
        # OpenRouter reports an upstream failure after the stream began this way.
        raise LLMUnavailable("OpenRouter finish_reason=error", provider=_PROVIDER)
    if choice.message.refusal or finish_reason == "content_filter":
        raise LLMRefused(
            f"OpenRouter finish_reason={finish_reason}",
            provider=_PROVIDER,
            usage=completion_usage(completion),
        )
    return choice


def _messages(request: LLMRequest, model: str) -> list[ChatCompletionMessageParam]:
    messages: list[ChatCompletionMessageParam] = []
    if request.system is not None:
        messages.append(
            _system(
                request.system, cached=request.cache_system and _is_anthropic(model)
            )
        )
    for message in request.messages:
        messages.extend(_turn(message))
    return messages


def _is_anthropic(model: str) -> bool:
    return model.startswith("anthropic/")


def _system(text: str, *, cached: bool) -> ChatCompletionSystemMessageParam:
    if not cached:
        return ChatCompletionSystemMessageParam(role="system", content=text)
    # OpenRouter passes the breakpoint on to Anthropic; the key is not part of
    # the OpenAI type.
    part = cast(
        ChatCompletionContentPartTextParam,
        {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}},
    )
    return ChatCompletionSystemMessageParam(role="system", content=[part])


def _turn(message: LLMMessage) -> list[ChatCompletionMessageParam]:
    if isinstance(message.content, str):
        if message.role == "user":
            return [
                ChatCompletionUserMessageParam(role="user", content=message.content)
            ]
        if not message.content:
            # Some providers behind OpenRouter refuse an empty assistant
            # message that is not the last one.
            return []
        return [
            ChatCompletionAssistantMessageParam(
                role="assistant", content=message.content
            )
        ]
    if message.role == "user":
        return _user_turn(message.content)
    return _assistant_turn(message.content)


def _user_turn(blocks: Sequence[ContentBlock]) -> list[ChatCompletionMessageParam]:
    # The answers to the assistant's tool calls must come right after it, so
    # they go first and the turn's text and images after them.
    turn: list[ChatCompletionMessageParam] = [
        ChatCompletionToolMessageParam(
            role="tool",
            tool_call_id=block.tool_use_id,
            # The OpenAI shape has no error flag.
            content=f"Error: {block.content}" if block.is_error else block.content,
        )
        for block in blocks
        if isinstance(block, ToolResultBlock)
    ]
    # Anthropic behind OpenRouter refuses an empty text part.
    parts = [
        _part(block)
        for block in blocks
        if isinstance(block, ImageBlock)
        or (isinstance(block, TextBlock) and block.text)
    ]
    if parts:
        turn.append(ChatCompletionUserMessageParam(role="user", content=parts))
    return turn


def _part(block: TextBlock | ImageBlock) -> ChatCompletionContentPartParam:
    if isinstance(block, TextBlock):
        return ChatCompletionContentPartTextParam(type="text", text=block.text)
    data = base64.b64encode(block.data).decode("ascii")
    return ChatCompletionContentPartImageParam(
        type="image_url", image_url={"url": f"data:{block.media_type};base64,{data}"}
    )


def _assistant_turn(blocks: Sequence[ContentBlock]) -> list[ChatCompletionMessageParam]:
    text = "".join(block.text for block in blocks if isinstance(block, TextBlock))
    calls = [
        {
            "id": block.id,
            "type": "function",
            "function": {"name": block.name, "arguments": json.dumps(block.input)},
        }
        for block in blocks
        if isinstance(block, ToolUseBlock)
    ]
    reasoning = [
        block.data
        for block in blocks
        if isinstance(block, ProviderBlock) and block.provider is _PROVIDER
    ]
    # A turn left with nothing (the other provider's reasoning alone) is
    # dropped. Our own reasoning alone, an answer cut while reasoning, goes
    # back with empty content: a continuation needs it.
    if not text and not calls and not reasoning:
        return []
    message: dict[str, object] = {
        "role": "assistant",
        "content": text or (None if calls else ""),
    }
    if calls:
        message["tool_calls"] = calls
    if reasoning:
        # One reasoning_details list per answer, sent back as it came; the
        # OpenAI type has no such key.
        message["reasoning_details"] = reasoning[0]
    return [cast(ChatCompletionAssistantMessageParam, message)]


def _tools(request: LLMRequest) -> list[ChatCompletionToolUnionParam] | Omit:
    if not request.tools:
        return omit
    # Strict: every field required, no extra keys; the SDK refuses a model
    # strict mode cannot express.
    return [
        pydantic_function_tool(tool.args, name=tool.name, description=tool.description)
        for tool in request.tools
    ]


def _tool_choice(request: LLMRequest) -> ChatCompletionToolChoiceOptionParam | Omit:
    return request.tool_choice if request.tools else omit


def _extra_body(request: LLMRequest, *, structured: bool) -> dict[str, object] | None:
    body: dict[str, object] = {}
    if structured:
        # Without require_parameters OpenRouter may route to a provider that
        # ignores response_format and answers free text.
        body["provider"] = {"require_parameters": True}
    if request.effort is not None:
        body["reasoning"] = {"effort": request.effort}
    return body or None


_STOPS: Mapping[str, LLMStop] = {"tool_calls": "tool_use", "length": "max_tokens"}


def _stop(reason: str | None) -> LLMStop:
    return _STOPS.get(reason or "", "end")


def _response(
    completion: ChatCompletion, *, structured: bool, latency_ms: int
) -> LLMResponse:
    choice = first_choice(completion)
    finish_reason: str | None = choice.finish_reason
    stop = _stop(finish_reason)
    tool_calls = choice.message.tool_calls or []
    if tool_calls and stop == "end":
        # Some providers behind OpenRouter report "stop" with tool calls; a
        # loop that reads `stop` would drop them. The raw reason is kept.
        stop = "tool_use"
    usage = completion_usage(completion)
    if stop == "max_tokens" and (structured or tool_calls):
        # Truncated JSON fails validation again on a repair attempt that has
        # even less room left, and a cut tool call carries half its arguments,
        # so neither is attempted. The answer was billed.
        raise LLMOutputError(
            (
                "tool call cut at max_tokens"
                if tool_calls
                else "structured output cut at max_tokens"
            ),
            provider=_PROVIDER,
            usage=usage,
        )
    content: list[AnswerBlock] = []
    reasoning = (choice.message.model_extra or {}).get("reasoning_details")
    if reasoning:
        content.append(
            ProviderBlock(provider=_PROVIDER, data=cast(JsonValue, reasoning))
        )
    if choice.message.content:
        content.append(TextBlock(choice.message.content))
    try:
        content.extend(_tool_use(call) for call in tool_calls)
    except (TypeError, ValueError):
        # Arguments that are not a JSON object: nothing in the answer can be
        # used, but it was billed.
        raise LLMUnavailable(
            "OpenRouter returned a malformed tool call",
            provider=_PROVIDER,
            usage=usage,
        ) from None
    return LLMResponse(
        content=content,
        usage=usage,
        model=completion.model,
        provider=_PROVIDER,
        stop=stop,
        stop_reason=finish_reason,
        latency_ms=latency_ms,
    )


def _tool_use(call: ChatCompletionMessageToolCallUnion) -> ToolUseBlock:
    if not isinstance(call, ChatCompletionMessageToolCall):
        raise TypeError("tool call is not a function call")
    arguments = json.loads(call.function.arguments)
    if not isinstance(arguments, dict):
        raise TypeError("tool arguments are not a JSON object")
    return ToolUseBlock(
        id=call.id,
        name=call.function.name,
        input=cast(dict[str, JsonValue], arguments),
    )


def completion_usage(completion: ChatCompletion) -> LLMUsage:
    usage = completion.usage
    if usage is None:
        return LLMUsage(input_tokens=0, output_tokens=0)
    details = usage.prompt_tokens_details
    cache_read = (details.cached_tokens or 0) if details is not None else 0
    cache_write = (details.cache_write_tokens or 0) if details is not None else 0
    # `cost` is OpenRouter's addition to the OpenAI usage object, in credits
    # (1 credit = 1 USD). Through str() so the float's binary noise does not
    # become Decimal digits.
    cost = (usage.model_extra or {}).get("cost")
    return LLMUsage(
        # prompt_tokens includes the cached ones; LLMUsage counts them apart.
        input_tokens=usage.prompt_tokens - cache_read - cache_write,
        output_tokens=usage.completion_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=None if cost is None else Decimal(str(cost)),
    )


def _describe(error: openai.OpenAIError) -> str:
    if isinstance(error, openai.APIStatusError):
        return (
            f"{type(error).__name__} status={error.status_code} "
            f"request_id={error.request_id}"
        )
    return type(error).__name__
