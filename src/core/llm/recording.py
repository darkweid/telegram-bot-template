"""Writing one `llm_usage` row per provider call."""

from typing import Any

import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from loggers import get_logger
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.errors.scrubbed import reportable
from src.core.llm.enums import LLMCallStatus, LLMProviderName
from src.core.llm.errors import LLMError, LLMOutputError
from src.core.llm.repository import LLMUsageRepository
from src.core.llm.types import LLMRequest, LLMResponse, LLMUsage
from src.core.update_context import get_update_context
from src.core.utils.datetime_utils import get_utc_now

logger = get_logger(__name__)


class LLMUsageRecorder:
    """Stores what a call cost, in a short transaction of its own.

    A session of its own, like the unreachable-user recorder: a call can be
    made outside any update (a worker task), and inside one the row must not
    commit, or be rolled back with, the handler's transaction.

    Best effort: `record` never raises. The answer is already paid for and
    on its way to the user; a failed write is logged and sent to Sentry.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        usage: LLMUsageRepository,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._usage = usage

    async def record(
        self,
        request: LLMRequest,
        response: LLMResponse | None,
        error: LLMError | None,
        *,
        provider: LLMProviderName,
        model: str,
        latency_ms: int,
    ) -> None:
        context = get_update_context()
        usage: LLMUsage | None
        if response is not None:
            usage = response.usage
        elif isinstance(error, LLMOutputError):
            # Answers that failed validation or were cut short were billed.
            usage = error.usage
        else:
            usage = None
        data: dict[str, Any] = {
            "created_at": get_utc_now(),
            "user_id": context.user_id if context is not None else None,
            "purpose": request.purpose,
            "provider": provider,
            "model": model,
            "input_tokens": usage.input_tokens if usage else 0,
            "output_tokens": usage.output_tokens if usage else 0,
            "cache_read_tokens": usage.cache_read_tokens if usage else 0,
            "cache_write_tokens": usage.cache_write_tokens if usage else 0,
            "cost_usd": usage.cost_usd if usage else None,
            "latency_ms": latency_ms,
            "status": LLMCallStatus.ERROR if error else LLMCallStatus.SUCCESS,
            "error_type": type(error).__name__ if error else None,
        }
        try:
            async with self._sessionmaker() as session:
                async with SQLAlchemyUnitOfWork(session) as uow:
                    await self._usage.create(uow.session, data)
                    await uow.commit()
        except Exception as failure:
            safe = reportable(failure)
            logger.error(
                "LLM usage not recorded: %s", type(failure).__name__, exc_info=safe
            )
            sentry_sdk.capture_exception(safe)
