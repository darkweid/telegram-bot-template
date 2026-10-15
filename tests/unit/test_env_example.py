from pathlib import Path
import re

from pydantic_settings import BaseSettings
import pytest

from src.main.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = (PROJECT_ROOT / ".env.example").read_text()

# Set by the image build; a value in .env would override the baked commit.
BUILD_TIME_KEYS = frozenset({"COMMIT_SHA"})


def _setting_names() -> list[str]:
    names = []
    for field in Config.model_fields.values():
        section = field.annotation
        assert isinstance(section, type) and issubclass(section, BaseSettings)
        names.extend(section.model_fields)
    return names


def _is_assigned(name: str) -> bool:
    """`NAME=...`, or `# NAME=...` for a key left at its default on purpose."""
    return re.search(rf"^(# )?{name}=", ENV_EXAMPLE, re.MULTILINE) is not None


@pytest.mark.parametrize(
    "name", [name for name in _setting_names() if name not in BUILD_TIME_KEYS]
)
def test_every_setting_is_in_env_example(name: str) -> None:
    """.env.example is the reference a new bot starts from; a setting missing
    there is one nobody learns about."""
    assert _is_assigned(name), f"{name} is missing from .env.example"


@pytest.mark.parametrize("name", sorted(BUILD_TIME_KEYS))
def test_build_time_settings_are_only_mentioned(name: str) -> None:
    """A copied .env must not override what the image build set."""
    assert not _is_assigned(name)
    assert re.search(rf"^#.*\b{name}\b", ENV_EXAMPLE, re.MULTILINE)
