"""Writing one `llm_usage` row per provider call."""

from collections.abc import Mapping
import time
from typing import Any

from pydantic import BaseModel
import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from loggers import get_logger
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.errors.scrubbed import reportable
from src.core.llm.enums import LLMCallStatus, LLMProviderName, ModelTier
from src.core.llm.errors import LLMError
from src.core.llm.interface import LLMClient
from src.core.llm.repository import LLMUsageRepository
from src.core.llm.types import LLMRequest, LLMResponse, LLMUsage, StructuredResponse
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
        # A failed call may still have been billed: a refusal, an answer that
        # failed validation, a repair cut short.
        usage: LLMUsage | None = None
        if response is not None:
            usage = response.usage
        elif error is not None:
            usage = error.usage
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


class RecordingLLMClient:
    """One provider's client with every call recorded in llm_usage, failed
    calls included, and with the exchange logged under LLM_LOG_CONTENT.

    Wraps one adapter, not the fallback pair: every attempt is a call the
    provider may bill, and only here are its provider and models known
    when it fails.
    """

    def __init__(
        self,
        inner: LLMClient,
        recorder: LLMUsageRecorder,
        *,
        provider: LLMProviderName,
        models: Mapping[ModelTier, str],
        log_content: bool = False,
    ) -> None:
        self.inner = inner
        self._recorder = recorder
        self._provider = provider
        self.models = models
        self._log_content = log_content

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        try:
            response = await self.inner.complete(request)
        except LLMError as error:
            await self._record_failure(request, error, started)
            raise
        await self._record_success(request, response)
        return response

    async def structured[T: BaseModel](
        self, request: LLMRequest, output: type[T]
    ) -> StructuredResponse[T]:
        started = time.perf_counter()
        try:
            result = await self.inner.structured(request, output)
        except LLMError as error:
            await self._record_failure(request, error, started)
            raise
        await self._record_success(request, result.response)
        return result

    async def _record_success(self, request: LLMRequest, response: LLMResponse) -> None:
        if self._log_content:
            logger.info(
                "LLM exchange for %s: %r -> %r",
                request.purpose,
                _transcript(request),
                response.text,
            )
        await self._recorder.record(
            request,
            response,
            None,
            provider=response.provider,
            model=response.model,
            latency_ms=response.latency_ms,
        )

    async def _record_failure(
        self, request: LLMRequest, error: LLMError, started: float
    ) -> None:
        if self._log_content:
            logger.info(
                "LLM exchange for %s: %r -> %s",
                request.purpose,
                _transcript(request),
                type(error).__name__,
            )
        await self._recorder.record(
            request,
            None,
            error,
            provider=self._provider,
            model=request.model or self.models[request.tier],
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _transcript(request: LLMRequest) -> list[tuple[str, str]]:
    system = [("system", request.system)] if request.system else []
    return system + [(message.role, message.text) for message in request.messages]
