import logging

from loggers import get_logger
from tests.helpers.logs import captured_logs


def test_records_of_a_non_propagating_logger_are_captured_and_released() -> None:
    logger = get_logger("tests.helpers.sample")
    level = logger.level
    with captured_logs("tests.helpers.sample") as records:
        logger.debug("one")
        logger.warning("two")
    logger.warning("after")
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.DEBUG, "one"),
        (logging.WARNING, "two"),
    ]
    assert logger.level == level
