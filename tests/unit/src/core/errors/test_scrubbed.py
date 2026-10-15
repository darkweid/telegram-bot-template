from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import DBAPIError

from src.core.errors.scrubbed import (
    ScrubbedError,
    describe_validation_error,
    reportable,
    scrubbed,
)


def test_scrubbed_keeps_the_traceback_and_drops_the_message_and_chain() -> None:
    try:
        raise ValueError("Key (email)=(a@b.c) already exists")
    except ValueError as original:
        replacement = scrubbed("users insert failed", original)
        assert replacement.__traceback__ is original.__traceback__
    assert isinstance(replacement, ScrubbedError)
    assert str(replacement) == "users insert failed"
    assert replacement.__cause__ is None


def test_validation_error_summary_names_fields_and_types_only() -> None:
    class Model(BaseModel):
        age: int

    try:
        Model(age="secret-value")
    except ValidationError as error:
        summary = describe_validation_error(error)
    assert summary == "Model: age: int_parsing"


class DriverError(Exception):
    pass


def test_a_driver_error_deep_in_the_chain_loses_its_detail() -> None:
    """Background tasks log through taskiq's receiver, which prints the whole
    chain; PostgreSQL's DETAIL names the row it refused."""
    driver = DBAPIError("INSERT", {}, DriverError("DETAIL: Key (id)=(4242) exists"))
    try:
        try:
            raise driver
        except DBAPIError as exc:
            raise RuntimeError("task failed") from exc
    except RuntimeError as outer:
        error = reportable(outer)
    assert error.__cause__ is not None
    assert isinstance(error.__cause__, ScrubbedError)
    assert "4242" not in str(error.__cause__)
