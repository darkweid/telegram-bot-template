from src.core.locales import RESPONSE_LANGUAGES, SUPPORTED_LOCALES


def test_every_supported_locale_has_a_response_language() -> None:
    """A locale added without a language name would fail every model call
    that answers in it, for the users who chose it."""
    assert set(RESPONSE_LANGUAGES) == set(SUPPORTED_LOCALES)
