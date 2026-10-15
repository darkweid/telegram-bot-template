"""Raw response bodies of both LLM APIs for ScriptedLLMServer: what the
provider sends, before any adapter reads it."""

import json

from pydantic import JsonValue

type JsonObject = dict[str, JsonValue]

ANTHROPIC_USAGE: JsonObject = {
    "input_tokens": 1000,
    "output_tokens": 200,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}
OPENROUTER_USAGE: JsonObject = {
    "prompt_tokens": 120,
    "completion_tokens": 30,
    "total_tokens": 150,
    "cost": 0.000123,
}


def anthropic_text(text: str) -> JsonObject:
    return {"type": "text", "text": text}


def anthropic_tool_use(
    name: str, input: JsonObject, *, id: str = "toolu_1"
) -> JsonObject:
    return {"type": "tool_use", "id": id, "name": name, "input": input}


def anthropic_thinking(
    thinking: str = "Weighing the options.", *, signature: str = "sig-1"
) -> JsonObject:
    return {"type": "thinking", "thinking": thinking, "signature": signature}


def anthropic_message(
    *content: JsonObject,
    stop_reason: str = "end_turn",
    model: str = "claude-sonnet-5-5",
    usage: JsonObject | None = None,
) -> JsonObject:
    blocks: list[JsonValue] = [*content]
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": None,
        "usage": usage or ANTHROPIC_USAGE,
    }


def openrouter_tool_call(
    name: str, arguments: JsonObject | str, *, id: str = "call_1"
) -> JsonObject:
    """A string `arguments` goes out as it is, for a malformed call."""
    return {
        "id": id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": (
                arguments if isinstance(arguments, str) else json.dumps(arguments)
            ),
        },
    }


def openrouter_completion(
    content: str | None = "Hello",
    *,
    tool_calls: list[JsonValue] | None = None,
    reasoning_details: list[JsonValue] | None = None,
    finish_reason: str = "stop",
    model: str = "anthropic/claude-sonnet-5.5",
    usage: JsonObject | None = None,
) -> JsonObject:
    message: JsonObject = {"role": "assistant", "content": content, "refusal": None}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if reasoning_details is not None:
        message["reasoning_details"] = reasoning_details
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        "usage": usage or OPENROUTER_USAGE,
    }
