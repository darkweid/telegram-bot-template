from typing import Protocol

from pydantic import BaseModel

from src.core.llm.types import LLMRequest, LLMResponse, StructuredResponse


class LLMClient(Protocol):
    """What domain code calls to reach a model.

    The provider adapters, the fallback and the usage-recording wrapper all
    implement it, so a use case never knows which of them it holds. Every
    failure surfaces as an `LLMError` subclass (src/core/llm/errors.py);
    nothing provider-specific escapes.
    """

    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def structured[T: BaseModel](
        self, request: LLMRequest, output: type[T]
    ) -> StructuredResponse[T]:
        """Answer as an instance of `output`: the provider is held to its JSON
        schema, and an answer that still fails validation raises
        LLMOutputError."""
        ...
