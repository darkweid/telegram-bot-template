"""Raw response bodies of both LLM APIs for ScriptedLLMServer: what the
provider sends, before any adapter reads it."""

import json
from typing import Any

ANTHROPIC_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 200,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}
OPENROUTER_USAGE = {
    "prompt_tokens": 120,
    "completion_tokens": 30,
    "total_tokens": 150,
    "cost": 0.000123,
}


def anthropic_text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def anthropic_tool_use(
    name: str, input: dict[str, Any], *, id: str = "toolu_1"
) -> dict[str, Any]:
    return {"type": "tool_use", "id": id, "name": name, "input": input}


def anthropic_thinking(
    thinking: str = "Weighing the options.", *, signature: str = "sig-1"
) -> dict[str, Any]:
    return {"type": "thinking", "thinking": thinking, "signature": signature}


def anthropic_message(
    *content: dict[str, Any],
    stop_reason: str = "end_turn",
    model: str = "claude-sonnet-5-5",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": list(content),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": None,
        "usage": usage or ANTHROPIC_USAGE,
    }


def openrouter_tool_call(
    name: str, arguments: dict[str, Any] | str, *, id: str = "call_1"
) -> dict[str, Any]:
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
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_details: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    model: str = "anthropic/claude-sonnet-5.5",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content, "refusal": None}
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
