import base64
from collections.abc import Callable
from decimal import Decimal
from functools import partial
import logging

import httpx2
import pytest

from src.core.llm.enums import LLMProviderName
from src.core.llm.errors import (
    LLMError,
    LLMOutputError,
    LLMRateLimited,
    LLMRefused,
    LLMTimeout,
    LLMUnavailable,
)
from src.core.llm.speech.interface import Transcript
from src.core.llm.speech.openrouter import (
    LANGUAGE_HINT,
    TRANSCRIBE_SYSTEM_PROMPT,
    OpenRouterTranscriber,
)
from tests.helpers.llm_bodies import (
    OPENROUTER_USAGE,
    JsonObject,
    openrouter_completion,
)
from tests.helpers.llm_server import ScriptedLLMServer
from tests.helpers.logs import captured_logs

AUDIO = b"OggS" + bytes(200)
AUDIO_BASE64 = base64.b64encode(AUDIO).decode()
MODEL = "google/gemini-3.8-flash"
API_KEY = "sk-or-test-key"
SECRET_TRANSCRIPT = "my blood test results came back"

# The transcriber writes no line of its own: the one line per call comes from
# report_llm_call. Our loggers do not propagate, so it is captured by name.
REPORTING_LOGGER = "src.core.llm.reporting"


@pytest.fixture
def server() -> ScriptedLLMServer:
    return ScriptedLLMServer()


def make_transcriber(
    server: ScriptedLLMServer, *, max_retries: int = 0
) -> OpenRouterTranscriber:
    return OpenRouterTranscriber(
        api_key=API_KEY,
        model=MODEL,
        max_tokens=4000,
        app_url="https://example.com/bot",
        app_title="Template Bot",
        timeout_seconds=7.0,
        max_retries=max_retries,
        http_client=server.http_client(),
    )


def completion(
    content: str | None = SECRET_TRANSCRIPT,
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    usage: JsonObject | None = None,
) -> JsonObject:
    return openrouter_completion(
        content, finish_reason=finish_reason, refusal=refusal, model=MODEL, usage=usage
    )


async def transcribe(
    transcriber: OpenRouterTranscriber,
    *,
    media_type: str = "audio/ogg",
    language: str | None = None,
) -> Transcript:
    return await transcriber.transcribe(
        AUDIO, media_type, language=language, purpose="voice"
    )


async def test_the_request_carries_one_audio_part_and_the_prompt(
    server: ScriptedLLMServer,
) -> None:
    """The model hears the audio only through this part, and a temperature
    the model refuses would turn every voice message into an outage."""
    server.reply(completion())
    transcriber = make_transcriber(server)

    await transcriber.transcribe(
        AUDIO, "audio/ogg; codecs=opus", language=None, purpose="voice"
    )

    [body] = server.bodies()
    assert set(body) == {"model", "messages", "max_tokens"}
    assert body["model"] == MODEL
    assert body["max_tokens"] == 4000
    assert "temperature" not in body
    assert body["messages"][0] == {
        "role": "system",
        "content": TRANSCRIBE_SYSTEM_PROMPT,
    }
    assert body["messages"][1]["role"] == "user"
    assert body["messages"][1]["content"] == [
        {
            "type": "input_audio",
            "input_audio": {"data": AUDIO_BASE64, "format": "ogg"},
        }
    ]
    assert len(body["messages"]) == 2
    await transcriber.aclose()


async def test_a_language_hint_is_appended_to_the_prompt(
    server: ScriptedLLMServer,
) -> None:
    """A hint keeps a short Russian message from being heard as another
    language, while still allowing speech in a different one."""
    server.reply(completion())
    transcriber = make_transcriber(server)

    await transcribe(transcriber, language="Russian")

    system = server.bodies()[0]["messages"][0]["content"]
    assert system == (
        TRANSCRIBE_SYSTEM_PROMPT + " " + LANGUAGE_HINT.format(language="Russian")
    )
    await transcriber.aclose()


async def test_the_transcript_carries_text_usage_model_and_latency(
    server: ScriptedLLMServer,
) -> None:
    """The usage row records the model that answered and what it cost; the
    domain reads only the stripped text."""
    server.reply(
        openrouter_completion(
            "  Two hundred grams.\n", model="google/gemini-3.8-flash-001"
        )
    )
    transcriber = make_transcriber(server)

    transcript = await transcriber.transcribe(
        AUDIO, "audio/ogg", language=None, purpose="voice"
    )

    assert transcript.text == "Two hundred grams."
    assert transcript.usage.cost_usd == Decimal("0.000123")
    assert transcript.usage.input_tokens == 120
    assert transcript.usage.output_tokens == 30
    assert transcript.model == "google/gemini-3.8-flash-001"
    assert transcript.provider is LLMProviderName.OPENROUTER
    assert transcript.latency_ms >= 0
    await transcriber.aclose()


@pytest.mark.parametrize("content", ["   \n", None], ids=["blank", "null"])
async def test_blank_or_null_content_is_an_empty_transcript(
    server: ScriptedLLMServer, content: str | None
) -> None:
    """Silence is a normal outcome the domain decides on, not a failure."""
    server.reply(completion(content))
    transcriber = make_transcriber(server)

    transcript = await transcriber.transcribe(
        AUDIO, "audio/ogg", language=None, purpose="voice"
    )

    assert transcript.text == ""
    await transcriber.aclose()


async def test_a_missing_cost_stays_unknown(server: ScriptedLLMServer) -> None:
    """An unknown cost summed as zero would understate spending in /stats."""
    usage = {key: value for key, value in OPENROUTER_USAGE.items() if key != "cost"}
    server.reply(completion(usage=usage))
    transcriber = make_transcriber(server)

    transcript = await transcriber.transcribe(
        AUDIO, "audio/ogg", language=None, purpose="voice"
    )

    assert transcript.usage.cost_usd is None
    await transcriber.aclose()


async def test_a_transcript_cut_at_max_tokens_is_an_output_error(
    server: ScriptedLLMServer,
) -> None:
    """A cut transcript can turn "2400 calories" into "2400 cal"; it must
    fail, and the answer was billed."""
    server.reply(completion(finish_reason="length"))
    transcriber = make_transcriber(server)

    with pytest.raises(LLMOutputError) as raised:
        await transcribe(transcriber)

    assert raised.value.usage is not None
    assert raised.value.usage.output_tokens == 30
    await transcriber.aclose()


def _http_error(server: ScriptedLLMServer, status: int) -> None:
    server.reply({"error": {"message": SECRET_TRANSCRIPT}}, status=status)


def _timeout(server: ScriptedLLMServer) -> None:
    server.fail(httpx2.ReadTimeout)


def _no_choices(server: ScriptedLLMServer) -> None:
    server.reply({**completion(), "choices": []})


def _finish_error(server: ScriptedLLMServer) -> None:
    server.reply(completion(finish_reason="error"))


def _not_json(server: ScriptedLLMServer) -> None:
    server.reply_raw(b"<html>" + SECRET_TRANSCRIPT.encode(), content_type="text/html")


def _null_prompt_tokens(server: ScriptedLLMServer) -> None:
    server.reply(completion(usage={**OPENROUTER_USAGE, "prompt_tokens": None}))


def _refusal(server: ScriptedLLMServer) -> None:
    server.reply(completion(refusal="no"))


def _cut(server: ScriptedLLMServer) -> None:
    server.reply(completion(finish_reason="length"))


FAILURES = [
    pytest.param(partial(_http_error, status=429), LLMRateLimited, id="429"),
    pytest.param(partial(_http_error, status=401), LLMUnavailable, id="401"),
    pytest.param(partial(_http_error, status=500), LLMUnavailable, id="500"),
    pytest.param(_timeout, LLMTimeout, id="timeout"),
    pytest.param(_no_choices, LLMUnavailable, id="no-choices"),
    pytest.param(_finish_error, LLMUnavailable, id="finish-error"),
    pytest.param(_not_json, LLMUnavailable, id="not-json"),
    pytest.param(_null_prompt_tokens, LLMUnavailable, id="null-prompt-tokens"),
    pytest.param(_refusal, LLMRefused, id="refusal"),
]


@pytest.mark.parametrize(("script", "expected"), FAILURES)
async def test_failures_map_to_llm_errors(
    server: ScriptedLLMServer,
    script: Callable[[ScriptedLLMServer], None],
    expected: type[LLMError],
) -> None:
    """The status block, the error handler and the usage row all hang on
    LLMError; an SDK exception escaping would skip every one of them."""
    script(server)
    transcriber = make_transcriber(server)

    with pytest.raises(expected) as raised:
        await transcribe(transcriber)

    assert type(raised.value) is expected
    assert raised.value.__cause__ is None
    if expected is LLMRefused:
        assert raised.value.usage is not None
    await transcriber.aclose()


@pytest.mark.parametrize(
    ("script", "expected"),
    [*FAILURES, pytest.param(_cut, LLMOutputError, id="cut")],
)
async def test_nothing_secret_reaches_errors_or_logs(
    server: ScriptedLLMServer,
    script: Callable[[ScriptedLLMServer], None],
    expected: type[LLMError],
) -> None:
    """The key, the voice and what was said are the user's and the operator's
    secrets; logs and Sentry keep only numbers and class names."""
    script(server)
    transcriber = make_transcriber(server)

    with captured_logs(REPORTING_LOGGER) as records:
        with pytest.raises(expected) as raised:
            await transcribe(transcriber)

    texts = [str(raised.value), raised.value.detail or ""]
    texts += [record.getMessage() for record in records]
    for text in texts:
        assert API_KEY not in text
        assert AUDIO_BASE64 not in text
        assert SECRET_TRANSCRIPT not in text
    await transcriber.aclose()


async def test_a_successful_call_logs_no_content(server: ScriptedLLMServer) -> None:
    """The transcript is user data even when the call succeeds."""
    server.reply(completion())
    transcriber = make_transcriber(server)

    with captured_logs(REPORTING_LOGGER) as records:
        await transcribe(transcriber)

    for record in records:
        line = record.getMessage()
        assert SECRET_TRANSCRIPT not in line
        assert AUDIO_BASE64 not in line
        assert API_KEY not in line
    await transcriber.aclose()


async def test_each_call_writes_one_line(server: ScriptedLLMServer) -> None:
    """One line per call is what /stats-style log queries and Sentry
    breadcrumbs count; a caller bug is no call and leaves no line."""
    server.reply(completion())
    server.reply(completion(refusal="no"))
    transcriber = make_transcriber(server)

    with captured_logs(REPORTING_LOGGER) as records:
        await transcribe(transcriber)
    [record] = records
    assert record.levelno == logging.INFO
    assert record.getMessage().startswith(
        f"LLM call provider=openrouter model={MODEL} purpose=voice outcome=ok"
    )

    with captured_logs(REPORTING_LOGGER) as records:
        with pytest.raises(LLMRefused):
            await transcribe(transcriber)
    [record] = records
    assert "outcome=LLMRefused" in record.getMessage()
    assert f"model={MODEL}" in record.getMessage()

    requests_before = len(server.requests)
    with captured_logs(REPORTING_LOGGER) as records:
        with pytest.raises(ValueError):
            await transcribe(transcriber, media_type="text/plain")
    assert records == []
    assert len(server.requests) == requests_before
    await transcriber.aclose()


async def test_bad_input_sends_no_request(server: ScriptedLLMServer) -> None:
    """A caller bug must not be paid for or read as an outage."""
    transcriber = make_transcriber(server)

    with pytest.raises(ValueError):
        await transcriber.transcribe(b"", "audio/ogg", language=None, purpose="voice")

    assert server.requests == []
    await transcriber.aclose()


async def test_aclose_closes_the_sdk_client(server: ScriptedLLMServer) -> None:
    """The container closes the transcriber on shutdown; open sockets would
    leak and warn under filterwarnings=error."""
    http_client = server.http_client()
    transcriber = OpenRouterTranscriber(
        api_key=API_KEY,
        model=MODEL,
        max_tokens=4000,
        app_url="",
        app_title="",
        timeout_seconds=7.0,
        max_retries=0,
        http_client=http_client,
    )

    await transcriber.aclose()

    assert http_client.is_closed
