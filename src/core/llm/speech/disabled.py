from src.core.llm.speech.errors import TranscriberNotConfigured
from src.core.llm.speech.interface import Transcript, check_transcription_input


class DisabledTranscriber:
    """The Transcriber bound while TRANSCRIBE_MODEL is empty.

    A bad call is still refused as a `ValueError` first, so a caller's bug is
    visible with the feature off.
    """

    async def transcribe(
        self, audio: bytes, media_type: str, *, language: str | None, purpose: str
    ) -> Transcript:
        check_transcription_input(audio, media_type, language=language, purpose=purpose)
        raise TranscriberNotConfigured(f"TRANSCRIBE_MODEL is empty; {purpose} needs it")
