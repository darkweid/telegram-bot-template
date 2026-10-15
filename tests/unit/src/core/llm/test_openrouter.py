from decimal import Decimal
import json
import logging
from typing import Any

import httpx2
from pydantic import BaseModel, field_validator
import pytest
import sentry_sdk

from src.core.llm.enums import LLMProviderName
from src.core.llm.errors import (
    LLMOutputError,
    LLMRateLimited,
    LLMRefused,
    LLMTimeout,
    LLMUnavailable,
)
from src.core.llm.openrouter import OpenRouterLLMClient
from src.core.llm.types import LLMMessage, LLMRequest
from tests.helpers.llm_server import ScriptedLLMServer
from tests.helpers.logs import captured_logs

SECRET_PROMPT = "my diary entry about the surgery"


class Summary(BaseModel):
    title: str
    points: list[str]

    @field_validator("points")
    @classmethod
    def not_empty(cls, points: list[str]) -> list[str]:
        if not points:
            raise ValueError("at least one point")
        return points


def completion(
    content: str | None = "Hello",
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    model: str = "google/gemini-3.8-flash",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "refusal": refusal,
                },
            }
        ],
        "usage": (
            usage
            if usage is not None
            else {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "cost": 0.000123,
                "prompt_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 0},
            }
        ),
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
    server: ScriptedLLMServer,
    *,
    max_retries: int = 0,
    app_url: str = "https://example.com/bot",
    app_title: str = "Template Bot",
) -> OpenRouterLLMClient:
    return OpenRouterLLMClient(
        api_key="sk-or-not-real",
        model="google/gemini-3.8-flash",
        app_url=app_url,
        app_title=app_title,
        timeout_seconds=7.0,
        max_retries=max_retries,
        http_client=server.http_client(),
    )


async def test_complete_sends_the_conversation_and_reads_usage_and_cost(
    server: ScriptedLLMServer,
) -> None:
    """The fallback path is only useful if its answer and its bill come back
    the way the primary's do: cached tokens apart, cost as an exact Decimal."""
    server.reply(completion())
    client = make_client(server)

    response = await client.complete(request())

    [sent] = server.requests
    assert sent.url == "https://openrouter.ai/api/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer sk-or-not-real"
    body = server.bodies()[0]
    assert body["model"] == "google/gemini-3.8-flash"
    assert body["max_tokens"] == 1024
    assert body["messages"] == [
        {"role": "system", "content": "Summarize."},
        {"role": "user", "content": SECRET_PROMPT},
    ]
    assert "temperature" not in body
    assert "response_format" not in body
    assert "provider" not in body
    assert response.text == "Hello"
    assert response.provider is LLMProviderName.OPENROUTER
    assert response.model == "google/gemini-3.8-flash"
    assert response.stop_reason == "stop"
    assert response.usage.input_tokens == 100
    assert response.usage.cache_read_tokens == 20
    assert response.usage.cache_write_tokens == 0
    assert response.usage.output_tokens == 30
    assert response.usage.cost_usd == Decimal("0.000123")
    assert response.latency_ms >= 0
    await client.aclose()


async def test_request_model_and_temperature_override_the_defaults(
    server: ScriptedLLMServer,
) -> None:
    """A use case that needs another model or sampling must get exactly that,
    and one that does not must not send a temperature some models reject."""
    server.reply(completion(model="google/gemini-3-flash"))
    client = make_client(server)

    await client.complete(request(model="google/gemini-3-flash", temperature=0.2))

    body = server.bodies()[0]
    assert body["model"] == "google/gemini-3-flash"
    assert body["temperature"] == 0.2
    await client.aclose()


async def test_attribution_headers_and_timeout_come_from_config(
    server: ScriptedLLMServer,
) -> None:
    """OpenRouter ranks and identifies apps by these headers, and the timeout
    bounds how long an update waits on a slow provider."""
    server.reply(completion())
    client = make_client(server)

    await client.complete(request())

    [sent] = server.requests
    assert sent.headers["http-referer"] == "https://example.com/bot"
    assert sent.headers["x-title"] == "Template Bot"
    assert sent.extensions["timeout"]["read"] == 7.0
    await client.aclose()


async def test_empty_attribution_is_not_sent(server: ScriptedLLMServer) -> None:
    """A blank header is worse than none: OpenRouter would list an app with no name."""
    server.reply(completion())
    client = make_client(server, app_url="", app_title="")

    await client.complete(request())

    [sent] = server.requests
    assert "http-referer" not in sent.headers
    assert "x-title" not in sent.headers
    await client.aclose()


async def test_structured_asks_for_a_strict_schema_on_providers_that_honour_it(
    server: ScriptedLLMServer,
) -> None:
    """Without require_parameters OpenRouter may route to a provider that
    ignores response_format, and the answer would not be JSON at all."""
    server.reply(completion(summary_json(["a", "b"])))
    client = make_client(server)

    result = await client.structured(request(), Summary)

    body = server.bodies()[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert (
        body["response_format"]["json_schema"]["schema"]["additionalProperties"]
        is False
    )
    assert body["provider"] == {"require_parameters": True}
    assert result.output == Summary(title="Week", points=["a", "b"])
    assert result.response.usage.cost_usd == Decimal("0.000123")
    await client.aclose()


async def test_structured_repairs_an_invalid_answer_once(
    server: ScriptedLLMServer,
) -> None:
    """A validator stricter than the schema rejects a valid-JSON answer; one
    repair round with the errors usually fixes it, and both rounds are billed."""
    server.reply(completion(summary_json([])))
    server.reply(completion(summary_json(["fixed"])))
    client = make_client(server)

    with captured_logs("src.core.llm.reporting") as records:
        result = await client.structured(request(), Summary)

    assert result.output.points == ["fixed"]
    repair_messages = server.bodies()[1]["messages"]
    assert repair_messages[:2] == server.bodies()[0]["messages"]
    assert repair_messages[2] == {"role": "assistant", "content": summary_json([])}
    assert repair_messages[3]["role"] == "user"
    assert "at least one point" in repair_messages[3]["content"]
    assert result.response.usage.output_tokens == 60
    assert result.response.usage.cost_usd == Decimal("0.000246")
    [line] = [record.getMessage() for record in records]
    assert "at least one point" not in line
    await client.aclose()


async def test_structured_gives_up_after_one_repair(server: ScriptedLLMServer) -> None:
    """An endless repair loop would spend money on every update; the user
    gets the output error after two answers."""
    server.reply(completion(summary_json([])))
    server.reply(completion("not json"))
    client = make_client(server)

    with pytest.raises(LLMOutputError) as raised:
        await client.structured(request(), Summary)

    assert len(server.requests) == 2
    assert raised.value.provider is LLMProviderName.OPENROUTER
    assert "not json" not in str(raised.value)
    assert raised.value.usage is not None
    assert raised.value.usage.output_tokens == 60
    assert raised.value.usage.cost_usd == Decimal("0.000246")
    await client.aclose()


async def test_structured_does_not_repair_a_truncated_answer(
    server: ScriptedLLMServer,
) -> None:
    """A repair of JSON cut at max_tokens has even less room and fails the
    same way, so the second paid request is skipped."""
    server.reply(completion('{"title": "We', finish_reason="length"))
    client = make_client(server)

    with pytest.raises(LLMOutputError) as raised:
        await client.structured(request(), Summary)

    assert len(server.requests) == 1
    assert raised.value.usage is not None
    assert raised.value.usage.cost_usd == Decimal("0.000123")
    await client.aclose()


async def test_a_truncated_repair_still_counts_the_first_answer(
    server: ScriptedLLMServer,
) -> None:
    """Both answers were billed; llm_usage must not drop the first one because
    the repair was cut at max_tokens."""
    server.reply(completion(summary_json([])))
    server.reply(completion('{"title": "We', finish_reason="length"))
    client = make_client(server)

    with pytest.raises(LLMOutputError) as raised:
        await client.structured(request(), Summary)

    assert len(server.requests) == 2
    assert raised.value.usage is not None
    assert raised.value.usage.output_tokens == 60
    assert raised.value.usage.cost_usd == Decimal("0.000246")
    await client.aclose()


@pytest.mark.parametrize(
    "answer",
    [
        completion(None, refusal="I can't help with that."),
        completion("", finish_reason="content_filter"),
    ],
)
async def test_a_refusal_is_refused_not_empty_text(
    server: ScriptedLLMServer, answer: dict[str, Any]
) -> None:
    """An empty answer shown as a summary would look like a bug; the user
    must be told the model declined."""
    server.reply(answer)
    client = make_client(server)

    with pytest.raises(LLMRefused):
        await client.complete(request())
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, LLMRateLimited),
        (401, LLMUnavailable),
        (402, LLMUnavailable),
        (500, LLMUnavailable),
        (503, LLMUnavailable),
    ],
)
async def test_http_errors_map_to_contract_errors(
    server: ScriptedLLMServer, status: int, expected: type[Exception]
) -> None:
    """The fallback client switches on these classes, and a revoked key or an
    empty balance must switch it like an outage does."""
    server.reply({"error": {"message": SECRET_PROMPT, "code": status}}, status=status)
    client = make_client(server)

    with pytest.raises(expected) as raised:
        await client.complete(request())

    assert raised.value.__cause__ is None
    assert SECRET_PROMPT not in str(raised.value)
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

    with pytest.raises(expected):
        await client.complete(request())
    await client.aclose()


async def test_sdk_retries_follow_the_configured_count(
    server: ScriptedLLMServer,
) -> None:
    """LLM_MAX_RETRIES is the only retry policy: the adapter adds none of its own."""
    server.reply(
        {"error": {"message": "busy"}}, status=503, headers={"retry-after-ms": "1"}
    )
    server.reply(completion())
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
    server.reply(completion("the answer about the surgery"))
    client = make_client(server)

    with captured_logs("src.core.llm.reporting") as records:
        await client.complete(request())

    [record] = records
    line = record.getMessage()
    assert record.levelno == logging.INFO
    for part in (
        "provider=openrouter",
        "model=google/gemini-3.8-flash",
        "purpose=note_summary",
        "outcome=ok",
        "input_tokens=100",
        "output_tokens=30",
        "cost_usd=0.000123",
        "latency_ms=",
    ):
        assert part in line
    assert "surgery" not in line
    [crumb] = breadcrumbs
    assert crumb["category"] == "llm"
    assert "surgery" not in repr(crumb)
    await client.aclose()


async def test_a_failed_call_is_logged_with_its_error_class(
    server: ScriptedLLMServer,
) -> None:
    """Failures cost latency and sometimes money; they need their line too."""
    server.reply({"error": {"message": "busy"}}, status=503)
    client = make_client(server)

    with captured_logs("src.core.llm.reporting") as records:
        with pytest.raises(LLMUnavailable):
            await client.complete(request())

    [record] = records
    assert record.levelno == logging.WARNING
    assert "outcome=LLMUnavailable" in record.getMessage()
    await client.aclose()


async def test_aclose_closes_the_http_client(server: ScriptedLLMServer) -> None:
    """The container closes the adapter on shutdown; open sockets would leak
    and warn under filterwarnings=error."""
    http_client = server.http_client()
    client = OpenRouterLLMClient(
        api_key="sk-or-not-real",
        model="google/gemini-3.8-flash",
        app_url="",
        app_title="",
        timeout_seconds=7.0,
        max_retries=0,
        http_client=http_client,
    )

    await client.aclose()

    assert http_client.is_closed
