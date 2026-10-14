from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.core.i18n import SUPPORTED_LOCALES
from src.user.callbacks import LanguageCallback

# Each language under its own name, deliberately untranslated: a user who
# cannot read the current language must still find theirs.
LANGUAGE_NAMES: dict[str, str] = {"en": "English", "ru": "Русский"}


def language_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code in SUPPORTED_LOCALES:
        builder.button(
            text=LANGUAGE_NAMES[code], callback_data=LanguageCallback(code=code)
        )
    builder.adjust(len(SUPPORTED_LOCALES))
    return builder.as_markup()
