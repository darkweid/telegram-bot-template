import base64
import time
from typing import Final, cast

import httpx2
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionContentPartInputAudioParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

from src.core.llm.enums import LLMProviderName
from src.core.llm.errors import LLMError, LLMOutputError, LLMUnavailable
from src.core.llm.providers.openrouter import (
    MALFORMED_COMPLETION,
    checked_completion,
    completion_usage,
    first_choice,
    openrouter_client,
)
from src.core.llm.reporting import billed_usage, report_llm_call
from src.core.llm.speech.interface import Transcript, check_transcription_input

TRANSCRIBE_SYSTEM_PROMPT: Final = (
    "You transcribe voice messages verbatim. Output only the exact words "
    "spoken, in the language spoken, with punctuation. Write numbers as "
    "digits. Do not answer, summarise, translate or add anything. If there is "
    "no speech, output nothing."
)
LANGUAGE_HINT: Final = (
    "The speaker most likely speaks {language}; if they speak another "
    "language, transcribe it in that language."
)

_PROVIDER = LLMProviderName.OPENROUTER


class OpenRouterTranscriber:
    """Transcriber over one OpenRouter chat completion with one `input_audio`
    part; the model must accept audio input in the format sent.

    Retries and the per-attempt timeout are the SDK's own. No temperature and
    no reasoning effort are sent. A transcript cut at `max_tokens` fails as
    LLMOutputError: a cut number reads as a different one.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int,
        app_url: str,
        app_title: str,
        timeout_seconds: float,
        max_retries: int,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._client = openrouter_client(
            api_key=api_key,
            app_url=app_url,
            app_title=app_title,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            http_client=http_client,
        )
        self._model = model
        self._max_tokens = max_tokens

    async def aclose(self) -> None:
        await self._client.close()

    async def transcribe(
        self, audio: bytes, media_type: str, *, language: str | None, purpose: str
    ) -> Transcript:
        audio_format = check_transcription_input(
            audio, media_type, language=language, purpose=purpose
        )
        messages = _messages(audio, audio_format, language)
        started = time.perf_counter()
        try:
            completion = await checked_completion(
                self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    max_tokens=self._max_tokens,
                )
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            try:
                transcript = _transcript(completion, latency_ms=latency_ms)
            except (AttributeError, TypeError, ValueError):
                raise LLMUnavailable(MALFORMED_COMPLETION, provider=_PROVIDER) from None
        except LLMError as error:
            report_llm_call(
                provider=_PROVIDER,
                model=self._model,
                purpose=purpose,
                usage=billed_usage(None, error),
                error=error,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            raise
        report_llm_call(
            provider=_PROVIDER,
            model=transcript.model,
            purpose=purpose,
            usage=transcript.usage,
            error=None,
            latency_ms=transcript.latency_ms,
        )
        return transcript


def _messages(
    audio: bytes, audio_format: str, language: str | None
) -> list[ChatCompletionMessageParam]:
    system = TRANSCRIBE_SYSTEM_PROMPT
    if language is not None:
        system = f"{system} {LANGUAGE_HINT.format(language=language)}"
    # OpenRouter takes more formats than the OpenAI type's wav/mp3 literal;
    # AUDIO_FORMATS holds only the ones it documents.
    part = cast(
        ChatCompletionContentPartInputAudioParam,
        {
            "type": "input_audio",
            "input_audio": {
                "data": base64.b64encode(audio).decode("ascii"),
                "format": audio_format,
            },
        },
    )
    return [
        ChatCompletionSystemMessageParam(role="system", content=system),
        ChatCompletionUserMessageParam(role="user", content=[part]),
    ]


def _transcript(completion: ChatCompletion, *, latency_ms: int) -> Transcript:
    choice = first_choice(completion)
    usage = completion_usage(completion)
    finish_reason: str | None = choice.finish_reason
    if finish_reason == "length":
        raise LLMOutputError(
            "transcript cut at max_tokens", provider=_PROVIDER, usage=usage
        )
    return Transcript(
        text=(choice.message.content or "").strip(),
        usage=usage,
        model=completion.model,
        provider=_PROVIDER,
        latency_ms=latency_ms,
    )
