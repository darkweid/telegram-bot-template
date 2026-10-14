from pathlib import Path

from aiogram.utils.i18n import I18n
import pytest

from src.core.i18n import LOCALES_DIR, N_, SUPPORTED_LOCALES, build_i18n, resolve_locale


@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        (("ru",), "ru"),
        (("en-US",), "en"),
        (("RU",), "ru"),
        (("ru_RU",), "ru"),
        (("pt-br",), "en"),
        ((None,), "en"),
        (("",), "en"),
        ((None, "ru"), "ru"),
        (("de", "ru"), "ru"),
        (("ru", "en"), "ru"),
    ],
)
def test_resolve_locale_takes_the_first_supported_candidate(
    candidates: tuple[str | None, ...], expected: str
) -> None:
    assert resolve_locale(*candidates, default="en") == expected


def test_resolve_locale_falls_back_to_the_given_default() -> None:
    assert resolve_locale("de", None, default="ru") == "ru"


def test_n_marker_returns_its_argument_untranslated() -> None:
    assert N_("Hello") == "Hello"


def test_locales_dir_points_at_the_repository_catalogs() -> None:
    assert (LOCALES_DIR / "ru" / "LC_MESSAGES" / "messages.po").is_file()


def test_build_i18n_loads_every_supported_locale(compiled_locales: Path) -> None:
    i18n = build_i18n(compiled_locales, default_locale="ru")
    assert isinstance(i18n, I18n)
    assert set(i18n.available_locales) == set(SUPPORTED_LOCALES)
    assert i18n.default_locale == "ru"


def test_build_i18n_refuses_a_missing_catalog(
    tmp_path: Path, compiled_locales: Path
) -> None:
    (tmp_path / "en" / "LC_MESSAGES").mkdir(parents=True)
    (tmp_path / "en" / "LC_MESSAGES" / "messages.mo").write_bytes(
        (compiled_locales / "en" / "LC_MESSAGES" / "messages.mo").read_bytes()
    )
    with pytest.raises(RuntimeError, match=r"^No compiled catalog for ru in "):
        build_i18n(tmp_path, default_locale="en")
