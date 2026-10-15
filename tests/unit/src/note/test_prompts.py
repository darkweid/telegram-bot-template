from pydantic import ValidationError
import pytest

from src.core.i18n import SUPPORTED_LOCALES
from src.note.prompts import (
    MAX_THEMES,
    RESPONSE_LANGUAGES,
    SUMMARY_MAX_CHARS,
    THEME_MAX_CHARS,
    NoteSummary,
    note_summary_system_prompt,
    note_summary_user_message,
)


def test_every_supported_locale_has_a_response_language() -> None:
    """A locale added without a language name would fail every /summarize of
    the users who chose it."""
    assert set(RESPONSE_LANGUAGES) == set(SUPPORTED_LOCALES)


def test_the_system_prompt_names_the_reply_language() -> None:
    """The prompt is English for every user; only this line decides the
    language of the summary."""
    assert "Write in Russian" in note_summary_system_prompt("ru")
    assert "Write in English" in note_summary_system_prompt("en")


def test_the_user_message_keeps_every_note_in_order() -> None:
    """The prompt says the notes come newest first; reordering them would
    make the model weigh old notes as recent."""
    message = note_summary_user_message(["buy milk", "call Ann"])
    assert message.index("buy milk") < message.index("call Ann")
    assert message.count("<note>") == 2


def test_a_summary_too_long_for_one_message_is_refused() -> None:
    """A refused output triggers the adapter's repair retry; an accepted one
    must fit a single Telegram message."""
    with pytest.raises(ValidationError):
        NoteSummary(summary="x" * (SUMMARY_MAX_CHARS + 1), themes=[])
    with pytest.raises(ValidationError):
        NoteSummary(summary="ok", themes=["theme"] * (MAX_THEMES + 1))
    with pytest.raises(ValidationError):
        NoteSummary(summary="ok", themes=["x" * (THEME_MAX_CHARS + 1)])


def test_a_blank_summary_is_refused_and_blank_themes_dropped() -> None:
    """An empty reply would read as a bot failure."""
    with pytest.raises(ValidationError):
        NoteSummary(summary="   ", themes=[])
    assert NoteSummary(summary=" ok ", themes=[" a ", " "]) == NoteSummary(
        summary="ok", themes=["a"]
    )


def test_the_length_limits_stay_out_of_the_json_schema() -> None:
    """Strict json_schema on some OpenRouter providers rejects maxLength and
    maxItems, and require_parameters would then find no provider at all."""
    schema = str(NoteSummary.model_json_schema())
    assert "maxLength" not in schema and "maxItems" not in schema
