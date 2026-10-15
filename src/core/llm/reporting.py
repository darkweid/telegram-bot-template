import logging

import sentry_sdk

from loggers import get_logger
from src.core.llm.enums import LLMProviderName
from src.core.llm.errors import LLMError, LLMOutputError
from src.core.llm.types import LLMRequest, LLMResponse, LLMUsage

logger = get_logger(__name__)


def report_llm_call(
    *,
    provider: LLMProviderName,
    model: str,
    request: LLMRequest,
    response: LLMResponse | None,
    error: LLMError | None,
    latency_ms: int,
) -> None:
    """Write the one log line and the one Sentry breadcrumb of an LLM call.

    Both carry the provider, model, purpose, token counts, cost and latency -
    never the prompt or the answer: those are user data. The exchange itself
    is logged only by RecordingLLMClient under LLM_LOG_CONTENT.
    """
    outcome = "ok" if error is None else type(error).__name__
    usage: LLMUsage | None
    if response is not None:
        usage = response.usage
    elif isinstance(error, LLMOutputError):
        # The answers that failed validation were billed all the same.
        usage = error.usage
    else:
        usage = None
    input_tokens = 0 if usage is None else usage.input_tokens
    output_tokens = 0 if usage is None else usage.output_tokens
    cache_read_tokens = 0 if usage is None else usage.cache_read_tokens
    cache_write_tokens = 0 if usage is None else usage.cache_write_tokens
    cost = "unknown" if usage is None or usage.cost_usd is None else str(usage.cost_usd)
    logger.log(
        logging.INFO if error is None else logging.WARNING,
        "LLM call provider=%s model=%s purpose=%s outcome=%s input_tokens=%d "
        "output_tokens=%d cache_read_tokens=%d cache_write_tokens=%d "
        "cost_usd=%s latency_ms=%d",
        provider,
        model,
        request.purpose,
        outcome,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_tokens,
        cost,
        latency_ms,
    )
    sentry_sdk.add_breadcrumb(
        category="llm",
        message="LLM call",
        level="info" if error is None else "warning",
        data={
            "provider": str(provider),
            "model": model,
            "purpose": request.purpose,
            "outcome": outcome,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cost_usd": cost,
            "latency_ms": latency_ms,
        },
    )
