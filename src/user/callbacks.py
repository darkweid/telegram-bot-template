from aiogram.filters.callback_data import CallbackData


class LanguageCallback(CallbackData, prefix="lang"):
    # Not validated here: an old keyboard may carry a code this release no
    # longer supports, and SetLocaleUseCase answers that properly.
    code: str
