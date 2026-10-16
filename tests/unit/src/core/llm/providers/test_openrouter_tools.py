import base64
from decimal import Decimal
import json
from typing import Any

from pydantic import BaseModel, JsonValue
import pytest

from src.core.llm.enums import LLMProviderName, ModelTier
from src.core.llm.errors import LLMOutputError, LLMUnavailable
from src.core.llm.providers.openrouter import OpenRouterLLMClient
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
from tests.helpers.llm_bodies import openrouter_completion, openrouter_tool_call
from tests.helpers.llm_server import ScriptedLLMServer

MODELS = {
    ModelTier.STRONG: "anthropic/claude-sonnet-5.5",
    ModelTier.FAST: "anthropic/claude-haiku-5.5",
}
REASONING: list[JsonValue] = [
    {"type": "reasoning.text", "text": "Weighing.", "signature": "s", "index": 0}
]


class Lookup(BaseModel):
    query: str
    limit: int | None


LOOKUP = LLMTool(name="lookup", description="Find a food by name.", args=Lookup)
ASK = LLMMessage(role="user", content="rice")


def request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {"purpose": "agent", "messages": [ASK], "tools": [LOOKUP]}
    fields.update(overrides)
    return LLMRequest(**fields)


@pytest.fixture
def server() -> ScriptedLLMServer:
    return ScriptedLLMServer()


def make_client(server: ScriptedLLMServer) -> OpenRouterLLMClient:
    return OpenRouterLLMClient(
        api_key="sk-or-not-real",
        models=MODELS,
        app_url="",
        app_title="",
        timeout_seconds=7.0,
        max_retries=0,
        http_client=server.http_client(),
    )


async def test_tools_go_out_strict_without_parallel_tool_calls(
    server: ScriptedLLMServer,
) -> None:
    """OpenRouter does not know parallel_tool_calls for Anthropic models."""
    server.reply(openrouter_completion())

    await make_client(server).complete(request())

    body = server.bodies()[0]
    [tool] = body["tools"]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "lookup"
    assert tool["function"]["strict"] is True
    assert tool["function"]["parameters"]["required"] == ["query", "limit"]
    assert body["tool_choice"] == "auto"
    assert "parallel_tool_calls" not in body
    assert "provider" not in body


async def test_a_request_without_tools_keeps_its_body(
    server: ScriptedLLMServer,
) -> None:
    server.reply(openrouter_completion())

    await make_client(server).complete(request(tools=()))

    body = server.bodies()[0]
    for key in ("tools", "tool_choice", "reasoning", "provider"):
        assert key not in body
    assert body["messages"] == [{"role": "user", "content": "rice"}]


async def test_every_tool_call_of_an_answer_becomes_a_block_in_order(
    server: ScriptedLLMServer,
) -> None:
    """Several calls in one answer are normal here; dropping one would leave
    the model waiting for a result it never gets."""
    server.reply(
        openrouter_completion(
            None,
            tool_calls=[
                openrouter_tool_call(
                    "lookup", {"query": "rice", "limit": None}, id="call_a"
                ),
                openrouter_tool_call(
                    "lookup", {"query": "beans", "limit": 2}, id="call_b"
                ),
            ],
            reasoning_details=REASONING,
            finish_reason="tool_calls",
        )
    )

    response = await make_client(server).complete(request())

    assert response.stop == "tool_use"
    assert response.tool_calls == (
        ToolUseBlock(
            id="call_a", name="lookup", input={"query": "rice", "limit": None}
        ),
        ToolUseBlock(id="call_b", name="lookup", input={"query": "beans", "limit": 2}),
    )
    assert response.content[0] == ProviderBlock(
        provider=LLMProviderName.OPENROUTER, data=REASONING
    )
    assert response.text == ""


@pytest.mark.parametrize("arguments", ["{not json", "[1, 2]"])
async def test_arguments_that_are_not_a_json_object_are_unavailable(
    server: ScriptedLLMServer, arguments: str
) -> None:
    """The answer was billed: /stats and llm_usage must count it even though
    nothing in it can be used."""
    server.reply(
        openrouter_completion(
            None,
            tool_calls=[openrouter_tool_call("lookup", arguments)],
            finish_reason="tool_calls",
        )
    )

    with pytest.raises(LLMUnavailable) as caught:
        await make_client(server).complete(request())

    assert caught.value.usage is not None
    assert caught.value.usage.input_tokens == 120
    assert caught.value.usage.output_tokens == 30
    assert caught.value.usage.cost_usd == Decimal("0.000123")


async def test_the_tool_loop_goes_back_in_the_openai_shape(
    server: ScriptedLLMServer,
) -> None:
    """Tool results must follow the assistant message that asked for them, and
    the reasoning goes back unchanged on that message."""
    server.reply(
        openrouter_completion(
            "Looking.",
            tool_calls=[
                openrouter_tool_call("lookup", {"query": "rice", "limit": None})
            ],
            reasoning_details=REASONING,
            finish_reason="tool_calls",
        )
    )
    server.reply(openrouter_completion("Done."))
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
                            tool_use_id="call_1", content="no such food", is_error=True
                        ),
                        TextBlock("Try beans."),
                    ],
                ),
            ]
        )
    )

    sent = server.bodies()[1]["messages"]
    assert sent[1] == {
        "role": "assistant",
        "content": "Looking.",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "lookup",
                    "arguments": json.dumps({"query": "rice", "limit": None}),
                },
            }
        ],
        "reasoning_details": REASONING,
    }
    assert sent[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "Error: no such food",
    }
    assert sent[3] == {
        "role": "user",
        "content": [{"type": "text", "text": "Try beans."}],
    }


async def test_another_providers_blocks_are_dropped(server: ScriptedLLMServer) -> None:
    server.reply(openrouter_completion())
    foreign = ProviderBlock(
        provider=LLMProviderName.ANTHROPIC, data={"type": "thinking"}
    )

    await make_client(server).complete(
        request(
            messages=[
                ASK,
                LLMMessage(role="assistant", content=[foreign]),
                LLMMessage(role="user", content="well?"),
            ]
        )
    )

    roles = [message["role"] for message in server.bodies()[0]["messages"]]
    assert roles == ["user", "user"]


async def test_an_image_goes_out_as_a_data_url(server: ScriptedLLMServer) -> None:
    server.reply(openrouter_completion())
    image = ImageBlock(media_type="image/jpeg", data=b"\xff\xd8bytes")

    await make_client(server).complete(
        request(messages=[LLMMessage(role="user", content=[image])])
    )

    [message] = server.bodies()[0]["messages"]
    url = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8bytes").decode("ascii")
    assert message["content"] == [{"type": "image_url", "image_url": {"url": url}}]


async def test_effort_goes_out_as_reasoning_next_to_require_parameters(
    server: ScriptedLLMServer,
) -> None:
    class Answer(BaseModel):
        summary: str

    server.reply(openrouter_completion('{"summary": "s"}'))

    await make_client(server).structured(request(tools=(), effort="low"), Answer)

    body = server.bodies()[0]
    assert body["reasoning"] == {"effort": "low"}
    assert body["provider"] == {"require_parameters": True}


async def test_cache_system_marks_the_system_prompt_of_an_anthropic_model(
    server: ScriptedLLMServer,
) -> None:
    server.reply(openrouter_completion())

    await make_client(server).complete(request(system="Plan meals.", cache_system=True))

    system = server.bodies()[0]["messages"][0]
    assert system == {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": "Plan meals.",
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


async def test_cache_system_leaves_other_models_alone(
    server: ScriptedLLMServer,
) -> None:
    """Other vendors cache repeated prefixes on their own and may refuse the key."""
    server.reply(openrouter_completion())

    await make_client(server).complete(
        request(
            system="Plan meals.", cache_system=True, model="google/gemini-3.8-flash"
        )
    )

    assert server.bodies()[0]["messages"][0] == {
        "role": "system",
        "content": "Plan meals.",
    }


async def test_a_tool_call_cut_at_max_tokens_is_an_output_error(
    server: ScriptedLLMServer,
) -> None:
    server.reply(
        openrouter_completion(
            None,
            tool_calls=[openrouter_tool_call("lookup", '{"query": "ri')],
            finish_reason="length",
        )
    )

    with pytest.raises(LLMOutputError) as caught:
        await make_client(server).complete(request())

    assert caught.value.usage is not None
    assert caught.value.usage.input_tokens == 120
    assert caught.value.usage.output_tokens == 30
    assert caught.value.usage.cost_usd == Decimal("0.000123")


async def test_a_tool_call_that_is_not_a_function_call_is_unavailable(
    server: ScriptedLLMServer,
) -> None:
    server.reply(
        openrouter_completion(
            None,
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "custom",
                    "custom": {"name": "lookup", "input": "rice"},
                }
            ],
            finish_reason="tool_calls",
        )
    )

    with pytest.raises(LLMUnavailable):
        await make_client(server).complete(request())


async def test_effort_on_a_plain_call_sends_no_provider_key(
    server: ScriptedLLMServer,
) -> None:
    server.reply(openrouter_completion())

    await make_client(server).complete(request(tools=(), effort="high"))

    body = server.bodies()[0]
    assert body["reasoning"] == {"effort": "high"}
    assert "provider" not in body


async def test_a_successful_tool_result_has_no_error_prefix(
    server: ScriptedLLMServer,
) -> None:
    server.reply(openrouter_completion())

    await make_client(server).complete(
        request(
            messages=[
                ASK,
                LLMMessage(
                    role="user",
                    content=[
                        ToolResultBlock(tool_use_id="call_1", content="rice, 130 kcal")
                    ],
                ),
            ]
        )
    )

    assert server.bodies()[0]["messages"][1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "rice, 130 kcal",
    }


async def test_tool_choice_none_is_sent_with_the_tools(
    server: ScriptedLLMServer,
) -> None:
    server.reply(openrouter_completion())

    await make_client(server).complete(request(tool_choice="none"))

    body = server.bodies()[0]
    assert body["tool_choice"] == "none"
    assert body["tools"]


async def test_an_empty_assistant_turn_is_not_sent(server: ScriptedLLMServer) -> None:
    """Anthropic behind OpenRouter answers 400 to an empty assistant message
    that is not the last one."""
    server.reply(openrouter_completion())

    await make_client(server).complete(
        request(
            messages=[
                ASK,
                LLMMessage(role="assistant", content=""),
                LLMMessage(role="user", content="well?"),
            ]
        )
    )

    roles = [message["role"] for message in server.bodies()[0]["messages"]]
    assert roles == ["user", "user"]


async def test_structured_output_takes_no_tools(server: ScriptedLLMServer) -> None:
    """Neither API combines a response schema with tools the same way; the
    caller's mistake is refused before anything is sent or billed."""

    class Answer(BaseModel):
        summary: str

    with pytest.raises(ValueError, match="tools"):
        await make_client(server).structured(request(), Answer)

    assert server.requests == []


async def test_tool_calls_under_finish_reason_stop_are_a_tool_use(
    server: ScriptedLLMServer,
) -> None:
    """Some providers behind OpenRouter report "stop" with tool calls; a loop
    that reads `stop` would drop the calls."""
    server.reply(
        openrouter_completion(
            None,
            tool_calls=[
                openrouter_tool_call("lookup", {"query": "rice", "limit": None})
            ],
            finish_reason="stop",
        )
    )

    response = await make_client(server).complete(request())

    assert response.stop == "tool_use"
    assert response.stop_reason == "stop"
    assert len(response.tool_calls) == 1


async def test_text_cut_at_max_tokens_is_still_an_answer(
    server: ScriptedLLMServer,
) -> None:
    server.reply(openrouter_completion("A long", finish_reason="length"))

    response = await make_client(server).complete(request())

    assert response.stop == "max_tokens"
    assert response.stop_reason == "length"
    assert response.text == "A long"


async def test_an_empty_text_block_is_not_sent(server: ScriptedLLMServer) -> None:
    """Anthropic behind OpenRouter refuses an empty text part; a user turn
    left with nothing but its tool results sends only those."""
    server.reply(openrouter_completion())

    await make_client(server).complete(
        request(
            messages=[
                ASK,
                LLMMessage(
                    role="assistant",
                    content=[
                        ToolUseBlock(
                            id="call_1", name="lookup", input={"query": "rice"}
                        )
                    ],
                ),
                LLMMessage(
                    role="user",
                    content=[
                        ToolResultBlock(tool_use_id="call_1", content="found"),
                        TextBlock(""),
                    ],
                ),
                LLMMessage(role="user", content=[TextBlock("")]),
            ]
        )
    )

    roles = [message["role"] for message in server.bodies()[0]["messages"]]
    assert roles == ["user", "assistant", "tool"]


async def test_a_reasoning_only_turn_goes_back_with_its_reasoning(
    server: ScriptedLLMServer,
) -> None:
    """An answer cut while still reasoning holds nothing but reasoning_details;
    dropping the turn would lose what a continuation needs."""
    server.reply(
        openrouter_completion(None, reasoning_details=REASONING, finish_reason="length")
    )
    server.reply(openrouter_completion("Done."))
    client = make_client(server)
    first = await client.complete(request())

    await client.complete(
        request(
            messages=[
                ASK,
                first.assistant_turn,
                LLMMessage(role="user", content="go on"),
            ]
        )
    )

    assert server.bodies()[1]["messages"][1] == {
        "role": "assistant",
        "content": "",
        "reasoning_details": REASONING,
    }
