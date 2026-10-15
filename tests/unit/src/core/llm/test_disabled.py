from pydantic import BaseModel
import pytest

from src.core.llm.disabled import DisabledLLMClient
from src.core.llm.errors import LLMNotConfigured
from src.core.llm.interface import LLMClient
from src.core.llm.types import LLMMessage, LLMRequest

REQUEST = LLMRequest(
    purpose="note_summary", messages=(LLMMessage(role="user", content="hi"),)
)


class Summary(BaseModel):
    text: str


async def test_a_disabled_client_refuses_a_completion() -> None:
    """With LLM_PROVIDER empty the bot still starts; the feature answers
    "not available" instead of crashing the update."""
    client: LLMClient = DisabledLLMClient()

    with pytest.raises(LLMNotConfigured) as excinfo:
        await client.complete(REQUEST)

    assert excinfo.value.provider is None
    assert "note_summary" in str(excinfo.value)


async def test_a_disabled_client_refuses_a_structured_call() -> None:
    client: LLMClient = DisabledLLMClient()

    with pytest.raises(LLMNotConfigured):
        await client.structured(REQUEST, Summary)
