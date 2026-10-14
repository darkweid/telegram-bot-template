"""Translations: gettext catalogs under locales/, served through aiogram's I18n.

Code marks strings with `gettext as _`, `lazy_gettext as __` and `ngettext`
from aiogram.utils.i18n; class attributes that hold a msgid use N_. The msgid
is the English text, so the English catalog may leave a msgstr empty.
"""

from pathlib import Path

from aiogram.utils.i18n import I18n

SUPPORTED_LOCALES: tuple[str, ...] = ("en", "ru")
DOMAIN = "messages"
# src/core/i18n/__init__.py -> the repository root, which holds locales/.
LOCALES_DIR = Path(__file__).resolve().parents[3] / "locales"


def N_(message: str) -> str:
    """Mark `message` for extraction without translating it.

    For msgids stored ahead of time - an exception's `message_key`, a command
    description - and translated later, once a locale is active.
    """
    return message


def resolve_locale(*candidates: str | None, default: str) -> str:
    """The first candidate we have a catalog for, else `default`.

    Telegram sends an IETF tag ("en", "en-US", "pt-br"); only its language
    part decides, so "en-US" picks "en" while "pt-br" moves on to the next
    candidate.
    """
    for candidate in candidates:
        if not candidate:
            continue
        language = candidate.replace("_", "-").split("-", 1)[0].strip().lower()
        if language in SUPPORTED_LOCALES:
            return language
    return default


def build_i18n(locales_dir: Path, default_locale: str) -> I18n:
    """Load the compiled catalogs, refusing to start without one of them.

    aiogram only loads locale directories that hold a .mo file and ignores
    the rest, so a missing catalog would otherwise surface as untranslated
    replies. A .po without its .mo makes aiogram itself raise.
    """
    i18n = I18n(path=locales_dir, default_locale=default_locale, domain=DOMAIN)
    missing = sorted(set(SUPPORTED_LOCALES) - set(i18n.available_locales))
    if missing:
        raise RuntimeError(
            f"No compiled catalog for {', '.join(missing)} in {locales_dir}; "
            "run `make i18n-compile`"
        )
    return i18n
