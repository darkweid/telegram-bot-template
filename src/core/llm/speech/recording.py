import asyncio
import time

from loggers import get_logger
from src.core.llm.enums import LLMProviderName
from src.core.llm.errors import LLMError, LLMNotConfigured
from src.core.llm.reporting import billed_usage
from src.core.llm.speech.interface import (
    Transcriber,
    Transcript,
    check_transcription_input,
)
from src.core.llm.usage.recording import (
    CANCELLED,
    LLMUsageRecorder,
    record_cancelled,
    record_ended,
)

logger = get_logger(__name__)


class RecordingTranscriber:
    """A transcriber with every attempt recorded in llm_usage, failed and
    cancelled ones included, and the transcription logged under
    LLM_LOG_CONTENT.

    Input `check_transcription_input` refuses raises its `ValueError` before
    the inner call and is not recorded, nor is `LLMNotConfigured`: neither
    reached a provider.
    """

    def __init__(
        self,
        inner: Transcriber,
        recorder: LLMUsageRecorder,
        *,
        provider: LLMProviderName,
        model: str,
        log_content: bool = False,
    ) -> None:
        self.inner = inner
        self._recorder = recorder
        self._provider = provider
        self._model = model
        self._log_content = log_content

    async def transcribe(
        self, audio: bytes, media_type: str, *, language: str | None, purpose: str
    ) -> Transcript:
        audio_format = check_transcription_input(
            audio, media_type, language=language, purpose=purpose
        )
        # Never the bytes: the line names the audio by format and size only.
        audio_label = f"<audio {audio_format} {len(audio)} bytes>"
        started = time.perf_counter()
        try:
            result = await self.inner.transcribe(
                audio, media_type, language=language, purpose=purpose
            )
        except LLMNotConfigured:
            raise
        except LLMError as error:
            self._log(purpose, audio_label, type(error).__name__)
            await record_ended(
                self._recorder,
                purpose=purpose,
                provider=self._provider,
                model=self._model,
                usage=billed_usage(None, error),
                error=error,
                latency_ms=_elapsed_ms(started),
            )
            raise
        except asyncio.CancelledError:
            # record_cancelled never raises: the cancel goes on as it came.
            self._log(purpose, audio_label, CANCELLED)
            await record_cancelled(
                self._recorder,
                purpose=purpose,
                provider=self._provider,
                model=self._model,
                latency_ms=_elapsed_ms(started),
            )
            raise
        self._log(purpose, audio_label, repr(result.text))
        await record_ended(
            self._recorder,
            purpose=purpose,
            provider=result.provider,
            model=result.model,
            usage=result.usage,
            error=None,
            latency_ms=result.latency_ms,
        )
        return result

    def _log(self, purpose: str, audio_label: str, outcome: str) -> None:
        if self._log_content:
            logger.info("Transcription for %s: %s -> %s", purpose, audio_label, outcome)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
