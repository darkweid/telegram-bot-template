from decimal import Decimal
from functools import partial
import time

import httpx2
import openai
from openai import AsyncOpenAI, Omit, omit
from openai.lib import type_to_response_format_param
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.completion_create_params import ResponseFormat
from pydantic import BaseModel

from src.core.llm.enums import LLMProviderName
from src.core.llm.errors import (
    LLMError,
    LLMOutputError,
    LLMRateLimited,
    LLMRefused,
    LLMTimeout,
    LLMUnavailable,
)
from src.core.llm.reporting import report_llm_call
from src.core.llm.structured import validated_with_repair
from src.core.llm.types import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    StructuredResponse,
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_PROVIDER = LLMProviderName.OPENROUTER


class OpenRouterLLMClient:
    """LLMClient over OpenRouter's OpenAI-compatible chat completions API.

    Retries (429, 5xx, connection errors) and the per-attempt timeout are the
    SDK's own, set from LLM_MAX_RETRIES and LLM_TIMEOUT_SECONDS. `cache_system`
    is not sent: the non-Anthropic models this adapter is meant for cache
    repeated prompt prefixes on their own. The cost comes from OpenRouter's
    `usage.cost`, so no price table is needed here.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        app_url: str,
        app_title: str,
        timeout_seconds: float,
        max_retries: int,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        # OpenRouter attributes traffic to an app by these two headers; an
        # empty value is left out rather than sent blank.
        attribution = {
            name: value
            for name, value in (("HTTP-Referer", app_url), ("X-Title", app_title))
            if value
        }
        # base_url is explicit so OPENAI_BASE_URL in the environment cannot
        # send the key somewhere else.
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            timeout=timeout_seconds,
            max_retries=max_retries,
            default_headers=attribution,
            http_client=http_client,
        )
        self._model = model

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self._model
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
        model = request.model or self._model
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
        try:
            completion = await self._client.chat.completions.create(
                model=model,
                messages=_messages(request),
                max_tokens=request.max_tokens,
                temperature=(
                    omit if request.temperature is None else request.temperature
                ),
                response_format=response_format,
                # Without require_parameters OpenRouter may route to a provider
                # that ignores response_format and answers free text.
                extra_body=(
                    {"provider": {"require_parameters": True}} if structured else None
                ),
            )
        except openai.APITimeoutError:
            raise LLMTimeout(
                "OpenRouter request timed out", provider=_PROVIDER
            ) from None
        except openai.RateLimitError as error:
            raise LLMRateLimited(_describe(error), provider=_PROVIDER) from None
        except openai.OpenAIError as error:
            # Chained from None: an SDK error's message carries the response
            # body, which can echo the request.
            raise LLMUnavailable(_describe(error), provider=_PROVIDER) from None
        except ValueError:
            # A 200 whose body is cut or empty: the JSON decoder's error holds
            # the whole body, which can quote the answer, so nothing is kept.
            raise LLMUnavailable(
                "OpenRouter returned an unreadable body", provider=_PROVIDER
            ) from None
        if not isinstance(completion, ChatCompletion):
            # The SDK hands back a non-JSON 200 body (an HTML error page from
            # a proxy) as a plain string.
            raise LLMUnavailable(
                "OpenRouter returned a body that is not JSON", provider=_PROVIDER
            ) from None
        latency_ms = int((time.perf_counter() - started) * 1000)
        return _response(completion, structured=structured, latency_ms=latency_ms)

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


def _messages(request: LLMRequest) -> list[ChatCompletionMessageParam]:
    messages: list[ChatCompletionMessageParam] = []
    if request.system is not None:
        messages.append(
            ChatCompletionSystemMessageParam(role="system", content=request.system)
        )
    messages.extend(_message(message) for message in request.messages)
    return messages


def _message(message: LLMMessage) -> ChatCompletionMessageParam:
    if message.role == "user":
        return ChatCompletionUserMessageParam(role="user", content=message.content)
    return ChatCompletionAssistantMessageParam(
        role="assistant", content=message.content
    )


def _response(
    completion: ChatCompletion, *, structured: bool, latency_ms: int
) -> LLMResponse:
    if not completion.choices:
        raise LLMUnavailable("OpenRouter returned no choices", provider=_PROVIDER)
    choice = completion.choices[0]
    finish_reason: str | None = choice.finish_reason
    if finish_reason == "error":
        # OpenRouter reports an upstream failure after the stream began this way.
        raise LLMUnavailable("OpenRouter finish_reason=error", provider=_PROVIDER)
    if choice.message.refusal or finish_reason == "content_filter":
        raise LLMRefused(
            f"OpenRouter finish_reason={finish_reason}", provider=_PROVIDER
        )
    if structured and finish_reason == "length":
        # Truncated JSON fails validation again on a repair attempt that has
        # even less room left, so it is not attempted. The answer was billed.
        raise LLMOutputError(
            "structured output cut at max_tokens",
            provider=_PROVIDER,
            usage=_usage(completion),
        )
    return LLMResponse(
        text=choice.message.content or "",
        usage=_usage(completion),
        model=completion.model,
        provider=_PROVIDER,
        stop_reason=finish_reason,
        latency_ms=latency_ms,
    )


def _usage(completion: ChatCompletion) -> LLMUsage:
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
