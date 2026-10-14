from src.core.errors.scrubbed import ScrubbedError, scrubbed


def test_scrubbed_keeps_the_traceback_and_drops_the_message_and_chain() -> None:
    try:
        raise ValueError("Key (email)=(a@b.c) already exists")
    except ValueError as original:
        replacement = scrubbed("users insert failed", original)
        assert replacement.__traceback__ is original.__traceback__
    assert isinstance(replacement, ScrubbedError)
    assert str(replacement) == "users insert failed"
    assert replacement.__cause__ is None
