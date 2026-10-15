"""The locales the bot ships catalogs for, and the language a model is told
to answer in for each.

Kept apart from `src.core.i18n`, whose package import loads aiogram: the
configuration and the scheduler's healthcheck need this module and nothing
else, so it stays stdlib-only.
"""

from collections.abc import Mapping
from typing import Final

SUPPORTED_LOCALES: tuple[str, ...] = ("en", "ru")

# The English name of every supported locale, as a prompt states it. In core,
# not in a domain's prompts: every domain that calls a model needs it, and it
# outlives the removal of any one of them.
RESPONSE_LANGUAGES: Final[Mapping[str, str]] = {"en": "English", "ru": "Russian"}
