from collections.abc import Awaitable, Callable
import dataclasses

from pydantic import BaseModel, ValidationError

from src.core.errors.scrubbed import describe_validation_error
from src.core.llm.enums import LLMProviderName
from src.core.llm.errors import LLMError, LLMOutputError
from src.core.llm.types import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    StructuredResponse,
)

# Sent back to the model after its answer failed validation. English like every
# prompt in the project: the language of the answer is set by the domain prompt.
REPAIR_INSTRUCTION = (
    "Your previous reply did not match the required JSON schema.\n"
    "Validation errors:\n{errors}\n"
    "Reply again with only the corrected JSON object."
)


def refuse_tools(request: LLMRequest) -> None:
    """Raises ValueError for a structured request with tools, before anything
    is sent."""
    if request.tools:
        # Neither API combines a response schema with tools the same way; this
        # is the caller's mistake and is refused before anything is billed.
        raise ValueError("structured output takes no tools")


async def validated_with_repair[T: BaseModel](
    send: Callable[[LLMRequest], Awaitable[LLMResponse]],
    request: LLMRequest,
    output: type[T],
    provider: LLMProviderName,
) -> StructuredResponse[T]:
    """Send `request`, validate the answer as `output`, repair it once.

    A schema-constrained answer can still fail validation: the providers drop
    what JSON Schema strict mode cannot carry (lengths, ranges), and a pydantic
    validator may reject what the schema allows. The validation errors go back
    to the model in one repair request; a second failure is LLMOutputError,
    whose detail names locations and error types only - the answer is model
    output and may quote user data, so it never reaches a log line. Its
    `usage` is what both answers cost. A request with tools raises
    ValueError before any call.
    """
    refuse_tools(request)
    first = await send(request)
    try:
        return StructuredResponse(
            output=output.model_validate_json(first.text), response=first
        )
    except ValidationError as error:
        rejection = error
    try:
        second = await send(_repair_request(request, first.text, rejection))
    except LLMError as error:
        # A repair answer cut at max_tokens or refused, or a repair request
        # that never got an answer: the first answer was billed either way.
        error.usage = (
            first.usage
            if error.usage is None
            else combine_usage(first.usage, error.usage)
        )
        raise
    response = dataclasses.replace(
        second,
        usage=combine_usage(first.usage, second.usage),
        latency_ms=first.latency_ms + second.latency_ms,
    )
    try:
        return StructuredResponse(
            output=output.model_validate_json(second.text), response=response
        )
    except ValidationError as error:
        raise LLMOutputError(
            describe_validation_error(error), provider=provider, usage=response.usage
        ) from None


def _repair_request(
    request: LLMRequest, rejected_output: str, error: ValidationError
) -> LLMRequest:
    # Locations and messages are enough to fix the answer; the rejected values
    # are already in the assistant turn right above.
    errors = error.json(include_url=False, include_context=False, include_input=False)
    return dataclasses.replace(
        request,
        messages=[
            *request.messages,
            LLMMessage(role="assistant", content=rejected_output),
            LLMMessage(role="user", content=REPAIR_INSTRUCTION.format(errors=errors)),
        ],
    )


def combine_usage(first: LLMUsage, second: LLMUsage) -> LLMUsage:
    """Usage of a call that took a repair attempt: both requests were billed.

    The cost stays unknown if either attempt's cost is unknown, rather than
    reporting part of it as the whole.
    """
    cost = (
        None
        if first.cost_usd is None or second.cost_usd is None
        else first.cost_usd + second.cost_usd
    )
    return LLMUsage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        cache_read_tokens=first.cache_read_tokens + second.cache_read_tokens,
        cache_write_tokens=first.cache_write_tokens + second.cache_write_tokens,
        cost_usd=cost,
    )
