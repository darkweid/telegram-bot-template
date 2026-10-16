import logging

from loggers import get_logger
from src.core.llm.enums import LLMProviderName
from src.core.llm.errors import LLMError
from src.core.llm.types import LLMUsage

logger = get_logger(__name__)


def billed_usage(usage: LLMUsage | None, error: LLMError | None) -> LLMUsage | None:
    """What a call was billed: the answer's usage, else the error's.

    A failed call may still have been billed: a refusal, an answer that
    failed validation, a repair cut short.
    """
    if usage is not None:
        return usage
    return None if error is None else error.usage


def report_llm_call(
    *,
    provider: LLMProviderName,
    model: str,
    purpose: str,
    usage: LLMUsage | None,
    error: LLMError | None,
    latency_ms: int,
    cancelled: bool = False,
) -> None:
    """Write the one log line of an LLM call.

    It carries the provider, model, purpose, token counts, cost and latency -
    never the prompt or the answer: those are user data. Sentry's logging
    integration keeps the line as the call's one breadcrumb. The exchange
    itself is logged only by RecordingLLMClient and RecordingTranscriber
    under LLM_LOG_CONTENT.

    `usage` is what the call was billed (`billed_usage`). `cancelled` is a
    call cut short by its caller (a deadline, a stopping bot): outcome
    `Cancelled`, logged without usage whatever `usage` holds.
    """
    if cancelled:
        outcome = "Cancelled"
        usage = None
    else:
        outcome = "ok" if error is None else type(error).__name__
    input_tokens = 0 if usage is None else usage.input_tokens
    output_tokens = 0 if usage is None else usage.output_tokens
    cache_read_tokens = 0 if usage is None else usage.cache_read_tokens
    cache_write_tokens = 0 if usage is None else usage.cache_write_tokens
    cost = "unknown" if usage is None or usage.cost_usd is None else str(usage.cost_usd)
    logger.log(
        logging.INFO if outcome == "ok" else logging.WARNING,
        "LLM call provider=%s model=%s purpose=%s outcome=%s input_tokens=%d "
        "output_tokens=%d cache_read_tokens=%d cache_write_tokens=%d "
        "cost_usd=%s latency_ms=%d",
        provider,
        model,
        purpose,
        outcome,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_tokens,
        cost,
        latency_ms,
    )
