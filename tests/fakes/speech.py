"""A Transcriber that answers from a queue, as FakeLLMClient does."""

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from src.core.llm.enums import LLMProviderName
from src.core.llm.speech.interface import Transcript, check_transcription_input
from src.core.llm.types import LLMUsage
from tests.fakes.llm import Hold


def transcript(
    text: str = "two hundred grams of rice", *, cost: Decimal | None = Decimal("0.0001")
) -> Transcript:
    return Transcript(
        text=text,
        usage=LLMUsage(input_tokens=20, output_tokens=6, cost_usd=cost),
        model="google/gemini-3.8-flash",
        provider=LLMProviderName.OPENROUTER,
        latency_ms=100,
    )


@dataclass(frozen=True, slots=True)
class TranscriptionCall:
    audio: bytes
    media_type: str
    language: str | None
    purpose: str


class FakeTranscriber:
    """Each call takes the next queued item: a transcript or an exception to
    raise. It refuses what the adapter refuses; an empty queue fails the
    test."""

    def __init__(self) -> None:
        self.calls: list[TranscriptionCall] = []
        self._queued: deque[Transcript | BaseException] = deque()
        self._holds: deque[Hold] = deque()

    def queue(self, result: Transcript | BaseException) -> None:
        self._queued.append(result)

    def hold(self) -> Hold:
        """The next call is recorded, sets `reached` and waits for
        `released`."""
        hold = Hold()
        self._holds.append(hold)
        return hold

    async def transcribe(
        self, audio: bytes, media_type: str, *, language: str | None, purpose: str
    ) -> Transcript:
        check_transcription_input(audio, media_type, language=language, purpose=purpose)
        self.calls.append(TranscriptionCall(audio, media_type, language, purpose))
        if self._holds:
            hold = self._holds.popleft()
            hold.reached.set()
            await hold.released.wait()
        if not self._queued:
            raise AssertionError("no transcript queued")
        item = self._queued.popleft()
        if isinstance(item, BaseException):
            raise item
        return item
