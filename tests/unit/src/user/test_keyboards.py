from src.core.i18n import SUPPORTED_LOCALES
from src.user.callbacks import LanguageCallback
from src.user.keyboards import LANGUAGE_NAMES, language_keyboard


def test_every_supported_language_has_a_name() -> None:
    # Adding a locale without a name must fail here, not in a user's /language.
    assert set(LANGUAGE_NAMES) == set(SUPPORTED_LOCALES)


def test_the_keyboard_is_one_row_with_a_button_per_language() -> None:
    [row] = language_keyboard().inline_keyboard
    assert [(b.text, b.callback_data) for b in row] == [
        (LANGUAGE_NAMES[code], LanguageCallback(code=code).pack())
        for code in SUPPORTED_LOCALES
    ]
