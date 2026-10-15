import base64
from typing import Any

from pydantic import BaseModel, field_validator
import pytest

from src.core.llm.anthropic import AnthropicLLMClient
from src.core.llm.enums import LLMProviderName, ModelTier
from src.core.llm.errors import LLMOutputError, LLMUnavailable
from src.core.llm.pricing import PricingTable
from src.core.llm.types import (
    ImageBlock,
    LLMMessage,
    LLMRequest,
    LLMTool,
    ProviderBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from tests.helpers.llm_bodies import (
    anthropic_message,
    anthropic_text,
    anthropic_thinking,
    anthropic_tool_use,
)
from tests.helpers.llm_server import ScriptedLLMServer

MODELS = {ModelTier.STRONG: "claude-sonnet-5-5", ModelTier.FAST: "claude-haiku-5-5"}


class Lookup(BaseModel):
    query: str
    limit: int | None

    @field_validator("query")
    @classmethod
    def short(cls, query: str) -> str:
        if len(query) > 100:
            raise ValueError("too long")
        return query


LOOKUP = LLMTool(name="lookup", description="Find a food by name.", args=Lookup)
ASK = LLMMessage(role="user", content="rice")


def request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {"purpose": "agent", "messages": [ASK], "tools": [LOOKUP]}
    fields.update(overrides)
    return LLMRequest(**fields)


@pytest.fixture
def server() -> ScriptedLLMServer:
    return ScriptedLLMServer()


def make_client(server: ScriptedLLMServer) -> AnthropicLLMClient:
    return AnthropicLLMClient(
        api_key="sk-ant-not-real",
        models=MODELS,
        pricing=PricingTable(),
        timeout_seconds=7.0,
        max_retries=0,
        http_client=server.http_client(),
    )


async def test_tools_go_out_strict_with_one_call_per_answer(
    server: ScriptedLLMServer,
) -> None:
    """Strict mode holds the call to the schema; a length limit in the schema
    would make some providers refuse it, so it stays a validator."""
    server.reply(anthropic_message(anthropic_text("ok")))

    await make_client(server).complete(request())

    body = server.bodies()[0]
    [tool] = body["tools"]
    assert tool["name"] == "lookup"
    assert tool["description"] == "Find a food by name."
    assert tool["strict"] is True
    assert tool["input_schema"]["required"] == ["query", "limit"]
    assert tool["input_schema"]["additionalProperties"] is False
    assert "maxLength" not in str(tool["input_schema"])
    assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}


async def test_tool_choice_none_forbids_calls(server: ScriptedLLMServer) -> None:
    server.reply(anthropic_message(anthropic_text("ok")))

    await make_client(server).complete(request(tool_choice="none"))

    assert server.bodies()[0]["tool_choice"] == {"type": "none"}


async def test_a_request_without_tools_keeps_its_body(
    server: ScriptedLLMServer,
) -> None:
    """Every existing caller sends text only; its body must not change."""
    server.reply(anthropic_message(anthropic_text("ok")))

    await make_client(server).complete(request(tools=()))

    body = server.bodies()[0]
    for key in ("tools", "tool_choice", "output_config", "cache_control", "thinking"):
        assert key not in body
    assert body["messages"] == [{"role": "user", "content": "rice"}]


async def test_a_tool_call_becomes_a_block_with_its_thinking(
    server: ScriptedLLMServer,
) -> None:
    server.reply(
        anthropic_message(
            anthropic_thinking(),
            anthropic_text("Looking it up."),
            anthropic_tool_use("lookup", {"query": "rice", "limit": None}),
            stop_reason="tool_use",
        )
    )

    response = await make_client(server).complete(request())

    assert response.stop == "tool_use"
    assert response.stop_reason == "tool_use"
    assert response.text == "Looking it up."
    assert response.tool_calls == (
        ToolUseBlock(
            id="toolu_1", name="lookup", input={"query": "rice", "limit": None}
        ),
    )
    assert response.content[0] == ProviderBlock(
        provider=LLMProviderName.ANTHROPIC, data=anthropic_thinking()
    )


async def test_the_tool_loop_goes_back_as_blocks_with_the_signed_thinking(
    server: ScriptedLLMServer,
) -> None:
    """Anthropic refuses a tool result whose assistant turn lost its signed
    thinking block, so it goes back exactly as it came."""
    server.reply(
        anthropic_message(
            anthropic_thinking(),
            anthropic_tool_use("lookup", {"query": "rice", "limit": None}),
            stop_reason="tool_use",
        )
    )
    server.reply(anthropic_message(anthropic_text("Rice: found.")))
    client = make_client(server)
    first = await client.complete(request())

    await client.complete(
        request(
            messages=[
                ASK,
                first.assistant_turn,
                LLMMessage(
                    role="user",
                    content=[
                        ToolResultBlock(
                            tool_use_id="toolu_1", content="no such food", is_error=True
                        )
                    ],
                ),
            ]
        )
    )

    sent = server.bodies()[1]["messages"]
    assert sent[1] == {
        "role": "assistant",
        "content": [
            anthropic_thinking(),
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "lookup",
                "input": {"query": "rice", "limit": None},
            },
        ],
    }
    assert sent[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "no such food",
                "is_error": True,
            }
        ],
    }


async def test_another_providers_blocks_are_dropped(server: ScriptedLLMServer) -> None:
    """After a fallback the history holds the other provider's reasoning,
    which this API cannot read; an assistant turn left with nothing is
    dropped, since an empty one is refused."""
    server.reply(anthropic_message(anthropic_text("ok")))
    foreign = ProviderBlock(provider=LLMProviderName.OPENROUTER, data=[{"type": "x"}])

    await make_client(server).complete(
        request(
            messages=[
                ASK,
                LLMMessage(role="assistant", content=[foreign, TextBlock("Hm.")]),
                LLMMessage(role="user", content="and?"),
                LLMMessage(role="assistant", content=[foreign]),
                LLMMessage(role="user", content="well?"),
            ]
        )
    )

    sent = server.bodies()[0]["messages"]
    assert sent[1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "Hm."}],
    }
    assert [message["role"] for message in sent] == [
        "user",
        "assistant",
        "user",
        "user",
    ]


async def test_an_image_goes_out_as_base64(server: ScriptedLLMServer) -> None:
    server.reply(anthropic_message(anthropic_text("A label.")))
    image = ImageBlock(media_type="image/png", data=b"\x89PNG-bytes")

    await make_client(server).complete(
        request(
            messages=[LLMMessage(role="user", content=[TextBlock("Read it."), image])]
        )
    )

    [message] = server.bodies()[0]["messages"]
    assert message["content"][1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(b"\x89PNG-bytes").decode("ascii"),
        },
    }


async def test_effort_and_the_output_format_share_one_output_config(
    server: ScriptedLLMServer,
) -> None:
    class Answer(BaseModel):
        summary: str

    server.reply(anthropic_message(anthropic_text('{"summary": "s"}')))

    await make_client(server).structured(request(tools=(), effort="low"), Answer)

    config = server.bodies()[0]["output_config"]
    assert config["effort"] == "low"
    assert config["format"]["type"] == "json_schema"


async def test_effort_alone_is_sent(server: ScriptedLLMServer) -> None:
    server.reply(anthropic_message(anthropic_text("ok")))

    await make_client(server).complete(request(effort="medium"))

    assert server.bodies()[0]["output_config"] == {"effort": "medium"}


async def test_cache_system_also_caches_the_history(server: ScriptedLLMServer) -> None:
    """A tool loop resends the whole history every step; without the top-level
    breakpoint only the system prompt would be read from the cache."""
    server.reply(anthropic_message(anthropic_text("ok")))

    await make_client(server).complete(request(system="Plan meals.", cache_system=True))

    body = server.bodies()[0]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_a_tool_call_cut_at_max_tokens_is_an_output_error(
    server: ScriptedLLMServer,
) -> None:
    """Its arguments are cut off: running the tool on them would act on half
    a request. The answer was billed."""
    server.reply(
        anthropic_message(
            anthropic_tool_use("lookup", {"query": "ri"}), stop_reason="max_tokens"
        )
    )

    with pytest.raises(LLMOutputError) as caught:
        await make_client(server).complete(request())

    assert caught.value.usage is not None


async def test_text_cut_at_max_tokens_is_still_an_answer(
    server: ScriptedLLMServer,
) -> None:
    server.reply(anthropic_message(anthropic_text("A long"), stop_reason="max_tokens"))

    response = await make_client(server).complete(request())

    assert response.stop == "max_tokens"
    assert response.text == "A long"


async def test_a_tool_call_whose_input_is_not_an_object_is_unavailable(
    server: ScriptedLLMServer,
) -> None:
    body = anthropic_message(stop_reason="tool_use")
    body["content"] = [
        {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": "rice"}
    ]
    server.reply(body)

    with pytest.raises(LLMUnavailable):
        await make_client(server).complete(request())
