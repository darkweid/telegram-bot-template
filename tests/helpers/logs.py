from collections.abc import Iterator
from contextlib import contextmanager
import logging


class _ListHandler(logging.Handler):
    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__(level=logging.DEBUG)
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def captured_logs(name: str) -> Iterator[list[logging.LogRecord]]:
    """Every record logger `name` handles while the block runs, DEBUG and up.

    Not caplog: get_logger turns propagation off, so the root logger caplog
    listens on never sees our records.
    """
    records: list[logging.LogRecord] = []
    handler = _ListHandler(records)
    logger = logging.getLogger(name)
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
