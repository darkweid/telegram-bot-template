from decimal import Decimal
import json
import logging
from typing import Any

import httpx2
from pydantic import BaseModel, field_validator
import pytest
import sentry_sdk

from src.core.llm.anthropic import AnthropicLLMClient
from src.core.llm.enums import LLMProviderName
from src.core.llm.errors import (
    LLMOutputError,
    LLMRateLimited,
    LLMRefused,
    LLMTimeout,
    LLMUnavailable,
)
from src.core.llm.pricing import PricingTable
from src.core.llm.types import LLMMessage, LLMRequest
from tests.helpers.llm_server import ScriptedLLMServer
from tests.helpers.logs import captured_logs

SECRET_PROMPT = "my diary entry about the surgery"
MODEL = "claude-sonnet-5-5"


class Summary(BaseModel):
    title: str
    points: list[str]

    @field_validator("points")
    @classmethod
    def not_empty(cls, points: list[str]) -> list[str]:
        if not points:
            raise ValueError("at least one point")
        return points


def message(
    text: str = "Hello",
    *,
    stop_reason: str = "end_turn",
    model: str = MODEL,
    content: list[dict[str, Any]] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content if content is not None else [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_input_tokens": 3000,
            "cache_creation_input_tokens": 400,
        },
    }


def summary_json(points: list[str]) -> str:
    return json.dumps({"title": "Week", "points": points})


def request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "purpose": "note_summary",
        "messages": [LLMMessage(role="user", content=SECRET_PROMPT)],
        "system": "Summarize.",
    }
    fields.update(overrides)
    return LLMRequest(**fields)


@pytest.fixture
def server() -> ScriptedLLMServer:
    return ScriptedLLMServer()


def make_client(
    server: ScriptedLLMServer, *, max_retries: int = 0
) -> AnthropicLLMClient:
    return AnthropicLLMClient(
        api_key="sk-ant-not-real",
        model=MODEL,
        pricing=PricingTable(),
        timeout_seconds=7.0,
        max_retries=max_retries,
        http_client=server.http_client(),
    )


async def test_complete_sends_the_conversation_and_prices_the_usage(
    server: ScriptedLLMServer,
) -> None:
    """The bill of every call is computed here, from all four token kinds; a
    wrong rate or a forgotten cache read misreports what the bot spends."""
    server.reply(message())
    client = make_client(server)

    response = await client.complete(request())

    [sent] = server.requests
    assert sent.url == "https://api.anthropic.com/v1/messages"
    assert sent.headers["x-api-key"] == "sk-ant-not-real"
    body = server.bodies()[0]
    assert body["model"] == MODEL
    assert body["max_tokens"] == 1024
    assert body["system"] == "Summarize."
    assert body["messages"] == [{"role": "user", "content": SECRET_PROMPT}]
    assert "output_config" not in body
    assert "temperature" not in body
    assert response.text == "Hello"
    assert response.provider is LLMProviderName.ANTHROPIC
    assert response.model == MODEL
    assert response.stop_reason == "end_turn"
    assert response.usage.input_tokens == 1000
    assert response.usage.output_tokens == 200
    assert response.usage.cache_read_tokens == 3000
    assert response.usage.cache_write_tokens == 400
    # claude-sonnet-5-5 at $2 / $10 / $2.50 / $0.20 per MTok:
    # 1000*2 + 200*10 + 400*2.5 + 3000*0.2 = 5600 per million tokens.
    assert response.usage.cost_usd == Decimal("0.0056")
    await client.aclose()


async def test_an_unpriced_model_reports_no_cost(server: ScriptedLLMServer) -> None:
    """A model missing from the table must not be billed as free."""
    server.reply(message(model="claude-unlisted-model"))
    client = make_client(server)

    response = await client.complete(request(model="claude-unlisted-model"))

    assert server.bodies()[0]["model"] == "claude-unlisted-model"
    assert response.usage.cost_usd is None
    await client.aclose()


async def test_only_the_text_blocks_make_the_answer(server: ScriptedLLMServer) -> None:
    """Sonnet 5.5 thinks by default; its thinking blocks are not for the user."""
    server.reply(
        message(
            content=[
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "Hel"},
                {"type": "text", "text": "lo"},
            ]
        )
    )
    client = make_client(server)

    response = await client.complete(request())

    assert response.text == "Hello"
    await client.aclose()


async def test_cache_system_marks_the_system_prompt_for_caching(
    server: ScriptedLLMServer,
) -> None:
    """A long fixed system prompt costs a tenth on a cache read; the flag is
    the only way a use case asks for it."""
    server.reply(message())
    server.reply(message())
    client = make_client(server)

    await client.complete(request(cache_system=True))
    await client.complete(request(system=None))

    cached, without_system = server.bodies()
    assert cached["system"] == [
        {"type": "text", "text": "Summarize.", "cache_control": {"type": "ephemeral"}}
    ]
    assert "system" not in without_system
    await client.aclose()


async def test_timeout_comes_from_config(server: ScriptedLLMServer) -> None:
    """The timeout bounds how long an update waits on a slow provider."""
    server.reply(message())
    client = make_client(server)

    await client.complete(request())

    assert server.requests[0].extensions["timeout"]["read"] == 7.0
    await client.aclose()


async def test_structured_asks_for_json_matching_the_model(
    server: ScriptedLLMServer,
) -> None:
    """Forced tool use is a 400 on Sonnet 5.5, so the schema goes through
    output_config; it must be strict or the API refuses it."""
    server.reply(message(summary_json(["a", "b"])))
    client = make_client(server)

    result = await client.structured(request(), Summary)

    body = server.bodies()[0]
    output_format = body["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["schema"]["additionalProperties"] is False
    assert set(output_format["schema"]["required"]) == {"title", "points"}
    assert "tool_choice" not in body
    assert result.output == Summary(title="Week", points=["a", "b"])
    assert result.response.usage.cost_usd == Decimal("0.0056")
    await client.aclose()


async def test_structured_repairs_an_invalid_answer_once(
    server: ScriptedLLMServer,
) -> None:
    """A validator stricter than the schema rejects a valid-JSON answer; one
    repair round with the errors usually fixes it, and both rounds are billed."""
    server.reply(message(summary_json([])))
    server.reply(message(summary_json(["fixed"])))
    client = make_client(server)

    with captured_logs("src.core.llm.reporting") as records:
        result = await client.structured(request(), Summary)

    assert result.output.points == ["fixed"]
    repair = server.bodies()[1]
    assert repair["system"] == "Summarize."
    assert repair["messages"][0] == {"role": "user", "content": SECRET_PROMPT}
    assert repair["messages"][1] == {"role": "assistant", "content": summary_json([])}
    assert repair["messages"][2]["role"] == "user"
    assert "at least one point" in repair["messages"][2]["content"]
    assert result.response.usage.output_tokens == 400
    assert result.response.usage.cost_usd == Decimal("0.0112")
    [line] = [record.getMessage() for record in records]
    assert "at least one point" not in line
    await client.aclose()


async def test_structured_gives_up_after_one_repair(server: ScriptedLLMServer) -> None:
    """An endless repair loop would spend money on every update; the user
    gets the output error after two answers."""
    server.reply(message(summary_json([])))
    server.reply(message("not json"))
    client = make_client(server)

    with captured_logs("src.core.llm.reporting") as records:
        with pytest.raises(LLMOutputError) as raised:
            await client.structured(request(), Summary)

    assert len(server.requests) == 2
    [line] = [record.getMessage() for record in records]
    assert "outcome=LLMOutputError" in line
    assert "output_tokens=400" in line
    assert "cost_usd=0.0112" in line
    assert raised.value.provider is LLMProviderName.ANTHROPIC
    assert "not json" not in str(raised.value)
    assert raised.value.usage is not None
    assert raised.value.usage.output_tokens == 400
    assert raised.value.usage.cost_usd == Decimal("0.0112")
    await client.aclose()


async def test_structured_does_not_repair_a_truncated_answer(
    server: ScriptedLLMServer,
) -> None:
    """A repair of JSON cut at max_tokens has even less room and fails the
    same way, so the second paid request is skipped."""
    server.reply(message('{"title": "We', stop_reason="max_tokens"))
    client = make_client(server)

    with pytest.raises(LLMOutputError) as raised:
        await client.structured(request(), Summary)

    assert len(server.requests) == 1
    assert raised.value.usage is not None
    assert raised.value.usage.cost_usd == Decimal("0.0056")
    await client.aclose()


async def test_a_refusal_is_refused_not_empty_text(server: ScriptedLLMServer) -> None:
    """A refusal is a 200 with stop_reason "refusal"; shown as an answer it
    would be an empty or half-written summary."""
    server.reply(
        message(
            "",
            stop_reason="refusal",
            stop_details={"type": "refusal", "category": "cyber", "explanation": None},
        )
    )
    client = make_client(server)

    with pytest.raises(LLMRefused) as raised:
        await client.complete(request())

    assert "category=cyber" in str(raised.value)
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "error_type", "expected"),
    [
        (429, "rate_limit_error", LLMRateLimited),
        (401, "authentication_error", LLMUnavailable),
        (400, "invalid_request_error", LLMUnavailable),
        (500, "api_error", LLMUnavailable),
        (529, "overloaded_error", LLMUnavailable),
    ],
)
async def test_http_errors_map_to_contract_errors(
    server: ScriptedLLMServer, status: int, error_type: str, expected: type[Exception]
) -> None:
    """The fallback client switches on these classes, and a revoked key must
    switch it like an outage does."""
    server.reply(
        {"type": "error", "error": {"type": error_type, "message": SECRET_PROMPT}},
        status=status,
        headers={"request-id": "req_1"},
    )
    client = make_client(server)

    with pytest.raises(expected) as raised:
        await client.complete(request())

    assert raised.value.__cause__ is None
    assert SECRET_PROMPT not in str(raised.value)
    assert f"status={status}" in str(raised.value)
    assert "request_id=req_1" in str(raised.value)
    await client.aclose()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [(httpx2.ReadTimeout, LLMTimeout), (httpx2.ConnectError, LLMUnavailable)],
)
async def test_transport_failures_map_to_contract_errors(
    server: ScriptedLLMServer,
    failure: type[httpx2.TransportError],
    expected: type[Exception],
) -> None:
    """A slow or unreachable provider must surface as the error the fallback
    client and the user message are built for, not as an SDK exception."""
    server.fail(failure)
    client = make_client(server)

    with pytest.raises(expected) as raised:
        await client.complete(request())

    assert raised.value.provider is LLMProviderName.ANTHROPIC
    await client.aclose()


async def test_sdk_retries_follow_the_configured_count(
    server: ScriptedLLMServer,
) -> None:
    """LLM_MAX_RETRIES is the only retry policy: the adapter adds none of its own."""
    server.reply(
        {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
        status=529,
        headers={"retry-after-ms": "1"},
    )
    server.reply(message())
    client = make_client(server, max_retries=1)

    response = await client.complete(request())

    assert response.text == "Hello"
    assert len(server.requests) == 2
    await client.aclose()


async def test_one_log_line_and_breadcrumb_without_content(
    server: ScriptedLLMServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prompts and answers are user data: the call is visible in logs and
    Sentry by its numbers only."""
    breadcrumbs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sentry_sdk, "add_breadcrumb", lambda **crumb: breadcrumbs.append(crumb)
    )
    server.reply(message("the answer about the surgery"))
    client = make_client(server)

    with captured_logs("src.core.llm.reporting") as records:
        await client.complete(request())

    [record] = records
    line = record.getMessage()
    assert record.levelno == logging.INFO
    for part in (
        "provider=anthropic",
        f"model={MODEL}",
        "purpose=note_summary",
        "outcome=ok",
        "input_tokens=1000",
        "output_tokens=200",
        "cache_read_tokens=3000",
        "cache_write_tokens=400",
        "cost_usd=0.0056",
        "latency_ms=",
    ):
        assert part in line
    assert "surgery" not in line
    [crumb] = breadcrumbs
    assert crumb["category"] == "llm"
    assert crumb["data"]["provider"] == "anthropic"
    assert "surgery" not in repr(crumb)
    await client.aclose()


async def test_a_failed_call_is_logged_with_its_error_class(
    server: ScriptedLLMServer,
) -> None:
    """Failures cost latency and sometimes money; they need their line too."""
    server.reply(
        {"type": "error", "error": {"type": "api_error", "message": "busy"}},
        status=500,
    )
    client = make_client(server)

    with captured_logs("src.core.llm.reporting") as records:
        with pytest.raises(LLMUnavailable):
            await client.complete(request())

    [record] = records
    assert record.levelno == logging.WARNING
    assert "outcome=LLMUnavailable" in record.getMessage()
    await client.aclose()


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"<html>bad gateway</html>", "text/html"),
        (b'{"id": "msg_1", "content": [{"type": "te', "application/json"),
        (b"", "application/json"),
        (
            b'{"type": "error", "error": {"type": "api_error", "message": "bad gateway"}}',
            "application/json",
        ),
        (b'{"id": "msg_1", "type": "message", "usage": null}', "application/json"),
        (
            b'{"id": "msg_1", "type": "message", "content": null, '
            b'"usage": {"input_tokens": 1, "output_tokens": 1}}',
            "application/json",
        ),
    ],
)
async def test_a_200_that_is_not_a_message_is_unavailable(
    server: ScriptedLLMServer, content: bytes, content_type: str
) -> None:
    """A proxy or an overloaded upstream can answer 200 with garbage; it must
    switch the fallback like any outage, not escape as a stdlib exception."""
    server.reply_raw(content, content_type=content_type)
    client = make_client(server)

    with captured_logs("src.core.llm.reporting") as records:
        with pytest.raises(LLMUnavailable) as raised:
            await client.complete(request())

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert "bad gateway" not in str(raised.value)
    [record] = records
    assert "outcome=LLMUnavailable" in record.getMessage()
    await client.aclose()


async def test_a_null_token_count_is_unavailable_even_unpriced(
    server: ScriptedLLMServer,
) -> None:
    """Without a price no arithmetic touches the counts, so a null one would
    reach the log line and the llm_usage insert and fail there, outside the
    adapter's guard."""
    answer = message(model="claude-unlisted-model")
    answer["usage"]["output_tokens"] = None
    server.reply(answer)
    client = make_client(server)

    with captured_logs("src.core.llm.reporting") as records:
        with pytest.raises(LLMUnavailable) as raised:
            await client.complete(request(model="claude-unlisted-model"))

    assert raised.value.__cause__ is None
    [record] = records
    assert "outcome=LLMUnavailable" in record.getMessage()
    await client.aclose()


async def test_aclose_closes_the_http_client(server: ScriptedLLMServer) -> None:
    """The container closes the adapter on shutdown; open sockets would leak
    and warn under filterwarnings=error."""
    http_client = server.http_client()
    client = AnthropicLLMClient(
        api_key="sk-ant-not-real",
        model=MODEL,
        pricing=PricingTable(),
        timeout_seconds=7.0,
        max_retries=0,
        http_client=http_client,
    )

    await client.aclose()

    assert http_client.is_closed
