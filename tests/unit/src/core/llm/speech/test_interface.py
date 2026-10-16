import pytest

from src.core.llm.speech.interface import MAX_AUDIO_BYTES, check_transcription_input


def test_a_known_type_maps_to_its_format() -> None:
    """The adapter sends the format name, not the MIME type."""
    assert (
        check_transcription_input(b"x", "audio/ogg", language=None, purpose="voice")
        == "ogg"
    )
    assert (
        check_transcription_input(b"x", "audio/mpeg", language=None, purpose="voice")
        == "mp3"
    )


def test_mime_parameters_and_case_are_ignored() -> None:
    """Telegram voice notes may arrive as `Audio/OGG; codecs=opus`."""
    assert (
        check_transcription_input(
            b"x", "Audio/OGG; codecs=opus", language=None, purpose="voice"
        )
        == "ogg"
    )


@pytest.mark.parametrize(
    ("audio", "media_type", "language", "purpose"),
    [
        (b"", "audio/ogg", None, "voice"),
        (b"x" * (MAX_AUDIO_BYTES + 1), "audio/ogg", None, "voice"),
        (b"x", "video/mp4", None, "voice"),
        (b"x", "audio/ogg", None, ""),
        (b"x", "audio/ogg", None, "p" * 65),
        (b"x", "audio/ogg", "", "voice"),
    ],
)
def test_bad_input_is_refused_before_any_call(
    audio: bytes, media_type: str, language: str | None, purpose: str
) -> None:
    """A caller bug must not be mistaken for a provider failure."""
    with pytest.raises(ValueError):
        check_transcription_input(audio, media_type, language=language, purpose=purpose)


def test_the_size_limit_is_inclusive() -> None:
    """Telegram lets the bot download exactly 20 MiB."""
    audio = b"x" * MAX_AUDIO_BYTES
    assert (
        check_transcription_input(audio, "audio/ogg", language=None, purpose="voice")
        == "ogg"
    )
