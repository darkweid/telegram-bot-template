import os

# Before any src.* import: importing it loads the config, which reads TESTING.
os.environ.setdefault("TESTING", "true")

from pathlib import Path

from aiogram.utils.i18n import I18n
import pytest

from src.core.i18n import LOCALES_DIR, build_i18n
from src.main.config import Config, get_settings
from tests.helpers.i18n import compile_catalogs


@pytest.fixture(scope="session")
def settings() -> Config:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="session")
def compiled_locales(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("locales")
    compile_catalogs(LOCALES_DIR, target)
    return target


@pytest.fixture(scope="session")
def i18n(compiled_locales: Path) -> I18n:
    return build_i18n(compiled_locales, default_locale="en")
