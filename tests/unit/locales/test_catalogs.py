"""The translation catalogs stay complete and in step with the code."""

from pathlib import Path
import re
import subprocess
import sysconfig

from babel.messages.catalog import Catalog
from babel.messages.pofile import read_po
import pytest

from src.core.i18n import DOMAIN, LOCALES_DIR
from src.core.locales import SUPPORTED_LOCALES

REPO_ROOT = Path(__file__).resolve().parents[3]
_PLACEHOLDER = re.compile(r"{(\w*)[^{}]*}")


def _catalog(path: Path, locale: str | None = None) -> Catalog:
    with path.open("rb") as file:
        return read_po(file, locale=locale)


def _po(locale: str) -> Catalog:
    return _catalog(LOCALES_DIR / locale / "LC_MESSAGES" / f"{DOMAIN}.po", locale)


def _ids(catalog: Catalog) -> set[str]:
    return {
        str(message.id[0] if isinstance(message.id, tuple) else message.id)
        for message in catalog
        if message.id
    }


@pytest.fixture(scope="module")
def extracted(tmp_path_factory: pytest.TempPathFactory) -> Catalog:
    """What `make i18n-extract` finds in the code right now."""
    pot = tmp_path_factory.mktemp("i18n") / f"{DOMAIN}.pot"
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-s",
            "i18n-extract",
            f"I18N_POT={pot}",
            # The pybabel installed next to this interpreter, venv activated or not.
            f"PYBABEL={Path(sysconfig.get_path('scripts')) / 'pybabel'}",
        ],
        check=False,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.fail(f"make i18n-extract failed:\n{result.stderr}")
    return _catalog(pot)


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_catalog_has_exactly_the_messages_in_the_code(
    locale: str, extracted: Catalog
) -> None:
    catalog = _po(locale)
    assert _ids(catalog) == _ids(extracted), "run `make i18n-update` and translate"
    assert not catalog.obsolete, "remove obsolete (#~) entries"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_catalog_has_no_fuzzy_entries(locale: str) -> None:
    fuzzy = [message.id for message in _po(locale) if message.id and message.fuzzy]
    assert fuzzy == []


def test_russian_catalog_translates_every_message() -> None:
    untranslated = [
        message.id
        for message in _po("ru")
        if message.id
        and (
            not message.string
            or (isinstance(message.string, tuple) and not all(message.string))
        )
    ]
    assert untranslated == []


def test_russian_catalog_declares_three_plural_forms() -> None:
    assert _po("ru").num_plurals == 3


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_translations_keep_the_placeholders(locale: str) -> None:
    for message in _po(locale):
        if not message.id:
            continue
        ids = message.id if isinstance(message.id, tuple) else (message.id,)
        strings = (
            message.string if isinstance(message.string, tuple) else (message.string,)
        )
        expected = set(_PLACEHOLDER.findall(ids[-1]))
        for string in strings:
            if string:  # an empty English msgstr falls back to the msgid
                assert set(_PLACEHOLDER.findall(string)) == expected, message.id
