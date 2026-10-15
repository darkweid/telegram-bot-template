import logging

import pytest
import sentry_sdk

from src.core.errors.exceptions import CoreException
from src.core.errors.reporting import report_core_exception
from tests.helpers.logs import captured_logs

LOGGER = "tests.core.errors.reporting"


class Expected(CoreException):
    log_level = "info"


class Serious(CoreException):
    log_level = "error"
    capture_to_sentry = True


@pytest.fixture
def sentry(monkeypatch: pytest.MonkeyPatch) -> list[BaseException]:
    captured: list[BaseException] = []

    def capture(error: BaseException) -> str:
        captured.append(error)
        return "event-1"

    monkeypatch.setattr(sentry_sdk, "capture_exception", capture)
    return captured


def test_an_expected_error_is_one_line_at_its_level_without_a_traceback(
    sentry: list[BaseException],
) -> None:
    """An outcome the code planned for is not a failure to investigate: no
    traceback, no Sentry event."""
    with captured_logs(LOGGER) as records:
        event_id = report_core_exception(
            logging.getLogger(LOGGER), Expected("note 7 is gone", name="<b>Ann</b>")
        )
    [record] = records
    assert (record.levelno, record.getMessage()) == (
        logging.INFO,
        "Expected: note 7 is gone",
    )
    assert record.exc_info is None
    assert event_id is None and sentry == []


def test_a_serious_error_keeps_its_traceback_and_reaches_sentry(
    sentry: list[BaseException],
) -> None:
    """From ERROR up somebody reads the trace; Sentry gets the event the
    error asked for, and its id is returned for the alert."""
    error = Serious()
    with captured_logs(LOGGER) as records:
        event_id = report_core_exception(logging.getLogger(LOGGER), error)
    [record] = records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Serious: -"
    assert record.exc_info is not None
    assert sentry == [error]
    assert event_id == "event-1"


def test_params_stay_out_of_the_line() -> None:
    """Params fill the user's text and can hold what they typed."""
    with captured_logs(LOGGER) as records:
        report_core_exception(
            logging.getLogger(LOGGER), Expected(name="my card PIN is 4931")
        )
    assert "4931" not in records[0].getMessage()
