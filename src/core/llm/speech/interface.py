from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from src.core.llm.enums import LLMProviderName
from src.core.llm.types import PURPOSE_MAX_LENGTH, LLMUsage

# The formats OpenRouter documents for `input_audio`.
AUDIO_FORMATS: Final[Mapping[str, str]] = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/flac": "flac",
}

# The Bot API's `getFile` limit: a larger voice message cannot be downloaded.
MAX_AUDIO_BYTES: Final = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Transcript:
    """`text` is stripped; an empty string means there was no speech. `model`
    is the one the provider named, which is what the usage row records."""

    text: str
    usage: LLMUsage
    model: str
    provider: LLMProviderName
    latency_ms: int


class Transcriber(Protocol):
    async def transcribe(
        self, audio: bytes, media_type: str, *, language: str | None, purpose: str
    ) -> Transcript:
        """Raise `ValueError` before any call on input that
        `check_transcription_input` refuses, and an `LLMError` subclass when
        the provider fails.

        `Transcript.text` is stripped; an empty string means there was no
        speech. `language` is an English language name used as a hint, or None.
        """
        ...


def check_transcription_input(
    audio: bytes, media_type: str, *, language: str | None, purpose: str
) -> str:
    """Return the audio format name for `media_type`.

    Raises `ValueError` for a caller bug, which must not be retried or read as
    a provider failure. Parameters and case of the MIME type are ignored.
    """
    audio_format = AUDIO_FORMATS.get(media_type.split(";", 1)[0].strip().lower())
    if audio_format is None:
        raise ValueError("unsupported audio type")
    if not audio:
        raise ValueError("audio is empty")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError(f"audio is larger than {MAX_AUDIO_BYTES} bytes")
    if not purpose:
        raise ValueError("purpose is empty")
    if len(purpose) > PURPOSE_MAX_LENGTH:
        raise ValueError(f"purpose is longer than {PURPOSE_MAX_LENGTH} characters")
    if language is not None and not language:
        raise ValueError("language is empty")
    return audio_format
