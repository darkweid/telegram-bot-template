from aiogram.utils.i18n import I18n
import pytest

from src.core.errors.exceptions import CoreException
from src.core.llm.errors import LLMNotConfigured
from src.core.llm.speech.disabled import DisabledTranscriber
from src.core.llm.speech.errors import TranscriberNotConfigured
from src.core.telegram.errors import error_reply_text


async def test_disabled_refuses_bad_input_before_saying_it_is_off() -> None:
    """A caller bug stays visible while the feature is off."""
    with pytest.raises(ValueError):
        await DisabledTranscriber().transcribe(
            b"x", "text/plain", language=None, purpose="voice"
        )


async def test_disabled_names_the_missing_setting() -> None:
    """The operator needs to know which key turns the feature on."""
    with pytest.raises(TranscriberNotConfigured) as caught:
        await DisabledTranscriber().transcribe(
            b"x", "audio/ogg", language=None, purpose="voice"
        )
    assert isinstance(caught.value, LLMNotConfigured)
    assert "TRANSCRIBE_MODEL" in caught.value.detail
    assert caught.value.capture_to_sentry is False


@pytest.mark.parametrize("locale", ["en", "ru"])
async def test_the_user_reads_the_not_available_text(i18n: I18n, locale: str) -> None:
    """A voice message to a bot without voice input must say the feature is
    off, in the user's language, not that something broke."""
    with pytest.raises(TranscriberNotConfigured) as caught:
        await DisabledTranscriber().transcribe(
            b"x", "audio/ogg", language=None, purpose="voice"
        )
    reply = error_reply_text(i18n, locale, caught.value)
    assert reply == error_reply_text(i18n, locale, LLMNotConfigured())
    assert reply != error_reply_text(i18n, locale, CoreException())
