import importlib
import pkgutil
from typing import get_args

from aiogram.utils.i18n import I18n
from babel.messages.pofile import read_po
import pytest

import src
from src.core.errors.exceptions import (
    CoreException,
    FilteringError,
    InfrastructureException,
    LogLevel,
)
from src.core.i18n import LOCALES_DIR


def _all_subclasses(cls: type[CoreException]) -> set[type[CoreException]]:
    found: set[type[CoreException]] = set()
    for subclass in cls.__subclasses__():
        found |= {subclass, *_all_subclasses(subclass)}
    return found


def _project_exceptions() -> list[type[CoreException]]:
    # Import every module under src/ so subclasses defined anywhere register.
    for module in pkgutil.walk_packages(src.__path__, prefix="src."):
        if not module.name.endswith("__main__"):
            importlib.import_module(module.name)
    return sorted(
        {CoreException, *_all_subclasses(CoreException)}, key=lambda c: c.__qualname__
    )


def _russian_msgids() -> dict[str, str]:
    with (LOCALES_DIR / "ru" / "LC_MESSAGES" / "messages.po").open("rb") as po:
        return {str(m.id): str(m.string) for m in read_po(po) if m.id}


@pytest.mark.parametrize(
    "exception_class", _project_exceptions(), ids=lambda c: c.__qualname__
)
def test_every_exception_has_a_translated_message(
    exception_class: type[CoreException],
) -> None:
    msgids = _russian_msgids()
    assert (
        exception_class.message_key in msgids
    ), "mark message_key with N_ and run i18n-update"
    assert msgids[exception_class.message_key]


@pytest.mark.parametrize(
    "exception_class", _project_exceptions(), ids=lambda c: c.__qualname__
)
def test_every_exception_declares_a_valid_log_level(
    exception_class: type[CoreException],
) -> None:
    assert exception_class.log_level in get_args(LogLevel)


def test_defaults() -> None:
    assert CoreException.log_level == "info"
    assert CoreException.capture_to_sentry is False
    assert InfrastructureException.log_level == "error"
    assert InfrastructureException.capture_to_sentry is True
    assert FilteringError.log_level == "warning"


def test_detail_is_kept_for_logs_and_params_for_the_message() -> None:
    error = CoreException("row 7 vanished", name="x")
    assert error.detail == "row 7 vanished"
    assert error.params == {"name": "x"}
    assert str(error) == "row 7 vanished"


def test_str_falls_back_to_the_class_name() -> None:
    assert str(InfrastructureException()) == "InfrastructureException"


def test_user_message_is_translated_in_the_active_locale(i18n: I18n) -> None:
    with i18n.context(), i18n.use_locale("ru"):
        assert (
            CoreException().user_message()
            == "Что-то пошло не так. Попробуйте ещё раз чуть позже."
        )
    with i18n.context(), i18n.use_locale("en"):
        assert CoreException().user_message() == CoreException.message_key


def test_user_message_fills_placeholders(i18n: I18n) -> None:
    class Custom(CoreException):
        message_key = "Hello, {name}"

    with i18n.context(), i18n.use_locale("en"):
        assert Custom(name="Ann").user_message() == "Hello, Ann"
