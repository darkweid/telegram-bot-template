# ruff: noqa: E402 -- TESTING must be set before any src.* import triggers config load
import os

os.environ.setdefault("TESTING", "true")

import pytest

from src.main.config import Config, get_settings


@pytest.fixture(scope="session")
def settings() -> Config:
    get_settings.cache_clear()
    return get_settings()
