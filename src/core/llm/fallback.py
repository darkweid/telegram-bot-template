"""Answering from a second provider while the first one is down."""

from dataclasses import replace

from pydantic import BaseModel

from loggers import get_logger
from src.core.errors.scrubbed import reportable
from src.core.llm.errors import LLMError, LLMRateLimited, LLMTimeout, LLMUnavailable
from src.core.llm.interface import LLMClient
from src.core.llm.types import LLMRequest, LLMResponse, StructuredResponse
from src.core.telegram.alerts import Alert, DeveloperAlerter
from src.core.update_context import get_update_context

logger = get_logger(__name__)

# Failures of the provider rather than of the request's content: another vendor
# can answer the same request. `LLMUnavailable` also covers a refused key and a
# malformed request, which switch too. A refusal or an unparsable answer would
# most likely repeat at the other vendor, and be paid for twice.
_SWITCH_ON = (LLMUnavailable, LLMTimeout, LLMRateLimited)


class FallbackLLMClient:
    """Sends each call to `primary` and, when that provider is down, timed
    out or rate limited, to `fallback`.

    The fallback answers with its own default model: a model named in the
    request is the primary vendor's id. Developers hear of the switch from
    the alerter, which sends the same failure once per cooldown.
    """

    def __init__(
        self, primary: LLMClient, fallback: LLMClient, alerter: DeveloperAlerter
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self._alerter = alerter

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            return await self.primary.complete(request)
        except _SWITCH_ON as error:
            await self._switching(request, error)
        return await self.fallback.complete(replace(request, model=None))

    async def structured[T: BaseModel](
        self, request: LLMRequest, output: type[T]
    ) -> StructuredResponse[T]:
        try:
            return await self.primary.structured(request, output)
        except _SWITCH_ON as error:
            await self._switching(request, error)
        return await self.fallback.structured(replace(request, model=None), output)

    async def _switching(self, request: LLMRequest, error: LLMError) -> None:
        provider = error.provider or "primary"
        logger.warning(
            "LLM %s failed for %s with %s, using the fallback",
            provider,
            request.purpose,
            type(error).__name__,
        )
        context = get_update_context()
        # Sent before the fallback is tried, so it leaves even when the
        # fallback fails as well.
        await self._alerter.alert_error(
            Alert(
                reportable(error),
                origin=f"llm {provider}",
                user_id=context.user_id if context is not None else None,
            )
        )
