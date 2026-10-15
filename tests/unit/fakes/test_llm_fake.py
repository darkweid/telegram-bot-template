from pydantic import BaseModel
import pytest

from src.core.llm.types import LLMMessage, LLMRequest, LLMTool
from tests.fakes.llm import FakeLLMClient, tool_call


class Lookup(BaseModel):
    query: str


class Answer(BaseModel):
    summary: str


async def test_structured_with_tools_is_refused_as_the_adapters_refuse_it() -> None:
    """A domain test that passed on the fake would fail in production."""
    fake = FakeLLMClient()
    fake.queue_structured(Answer(summary="s"))
    request = LLMRequest(
        purpose="agent",
        messages=[LLMMessage(role="user", content="rice")],
        tools=[LLMTool(name="lookup", description="Find a food.", args=Lookup)],
    )

    with pytest.raises(ValueError, match="structured output takes no tools"):
        await fake.structured(request, Answer)

    assert fake.calls == []


def test_a_default_call_id_depends_only_on_the_call() -> None:
    """Under random test order a process-wide counter would hand the same call
    a different id in every run."""
    assert tool_call("lookup", {"query": "rice"}).id == (
        tool_call("lookup", {"query": "rice"}).id
    )
    assert tool_call("lookup", {"query": "rice"}).id != (
        tool_call("lookup", {"query": "beans"}).id
    )
    assert tool_call("lookup", {"query": "rice"}).id != (
        tool_call("search", {"query": "rice"}).id
    )


def test_an_explicit_call_id_wins() -> None:
    assert tool_call("lookup", {"query": "rice"}, id="call_9").id == "call_9"
