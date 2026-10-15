"""The locales the bot ships catalogs for.

Kept apart from `src.core.i18n`, whose package import loads aiogram: the
configuration and the scheduler's healthcheck need this tuple and nothing else.
"""

SUPPORTED_LOCALES: tuple[str, ...] = ("en", "ru")
