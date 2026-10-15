import logging

from loggers import get_logger
from src.core.llm.enums import LLMProviderName
from src.core.llm.errors import LLMError
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
    """Write the one log line of an LLM call.

    It carries the provider, model, purpose, token counts, cost and latency -
    never the prompt or the answer: those are user data. Sentry's logging
    integration keeps the line as the call's one breadcrumb. The exchange
    itself is logged only by RecordingLLMClient under LLM_LOG_CONTENT.
    """
    outcome = "ok" if error is None else type(error).__name__
    # A failed call may still have been billed: a refusal, an answer that
    # failed validation, a repair cut short.
    usage: LLMUsage | None = None
    if response is not None:
        usage = response.usage
    elif error is not None:
        usage = error.usage
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
