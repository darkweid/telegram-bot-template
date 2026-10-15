from pydantic import BaseModel

from src.core.llm.errors import LLMNotConfigured
from src.core.llm.types import LLMRequest, LLMResponse, StructuredResponse


class DisabledLLMClient:
    """The LLMClient bound while LLM_PROVIDER is empty.

    The container still resolves every use case that needs a client, so the
    bot starts without any provider key; only an actual call fails, with a
    message the user can read.
    """

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise LLMNotConfigured(f"LLM_PROVIDER is empty; {request.purpose} needs it")

    async def structured[T: BaseModel](
        self, request: LLMRequest, output: type[T]
    ) -> StructuredResponse[T]:
        raise LLMNotConfigured(f"LLM_PROVIDER is empty; {request.purpose} needs it")
