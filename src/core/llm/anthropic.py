import dataclasses
from functools import partial
import time

import anthropic
from anthropic import AsyncAnthropic, Omit, omit, transform_schema
from anthropic.types import Message, MessageParam, OutputConfigParam, TextBlockParam
import httpx2
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
from src.core.llm.pricing import PricingTable
from src.core.llm.reporting import report_llm_call
from src.core.llm.structured import validated_with_repair
from src.core.llm.types import LLMRequest, LLMResponse, LLMUsage, StructuredResponse

ANTHROPIC_BASE_URL = "https://api.anthropic.com"

_PROVIDER = LLMProviderName.ANTHROPIC


class AnthropicLLMClient:
    """LLMClient over the Anthropic Messages API.

    Retries (408, 409, 429, 5xx, connection errors) and the per-attempt
    timeout are the SDK's own, set from LLM_MAX_RETRIES and
    LLM_TIMEOUT_SECONDS. The cost is computed from the response's usage with
    the price table: the API does not return one.

    `structured` asks for JSON through `output_config.format`, not forced
    tool use: claude-sonnet-5-5 and claude-opus-5-5 reject `tool_choice`
    "tool" and "any" with a 400. `LLMRequest.temperature` is not sent: the current
    models reject sampling parameters and the SDK no longer accepts them.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
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
        self._model = model
        self._pricing = pricing

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self._model
        started = time.perf_counter()
        try:
            response = await self._send(request, model=model, output_config=omit)
        except LLMError as error:
            self._report(request, model, started, None, error)
            raise
        self._report(request, model, started, response, None)
        return response

    async def structured[T: BaseModel](
        self, request: LLMRequest, output: type[T]
    ) -> StructuredResponse[T]:
        model = request.model or self._model
        # transform_schema makes the pydantic schema strict (no extra keys)
        # and drops constraints the API cannot enforce; pydantic still checks
        # them on the answer.
        output_config: OutputConfigParam = {
            "format": {"type": "json_schema", "schema": transform_schema(output)}
        }
        send = partial(self._send, model=model, output_config=output_config)
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
        output_config: OutputConfigParam | Omit,
    ) -> LLMResponse:
        started = time.perf_counter()
        try:
            message = await self._client.messages.create(
                model=model,
                max_tokens=request.max_tokens,
                system=_system(request),
                messages=[
                    MessageParam(role=item.role, content=item.content)
                    for item in request.messages
                ],
                output_config=output_config,
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
                structured=not isinstance(output_config, Omit),
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
        if structured and message.stop_reason == "max_tokens":
            # Truncated JSON fails validation again on a repair attempt that
            # has even less room left, so it is not attempted. The answer was
            # billed.
            raise LLMOutputError(
                "structured output cut at max_tokens", provider=_PROVIDER, usage=usage
            )
        return LLMResponse(
            # Thinking blocks are the model's reasoning, not the answer.
            text="".join(
                block.text for block in message.content if block.type == "text"
            ),
            usage=usage,
            model=message.model,
            provider=_PROVIDER,
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
