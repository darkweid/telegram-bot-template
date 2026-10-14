from pydantic import BaseModel, ValidationError

from src.core.errors.scrubbed import (
    ScrubbedError,
    describe_validation_error,
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
