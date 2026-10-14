from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError


class ScrubbedError(Exception):
    """Stands in for an exception whose message echoes user data.

    A driver's IntegrityError carries the PostgreSQL DETAIL (`Key (email)=(...)`,
    `Failing row contains (...)`) and a pydantic ValidationError prints every
    `input_value`; both reach a log line or a Sentry event through `str(exc)`
    and through the exception chain. This one carries a summary the caller
    built from safe fields and the original traceback. It starts with no
    chain: below a driver error every link repeats the DETAIL, below a
    pydantic error the chain is the caller's, so the caller decides.
    """


def scrubbed(summary: str, original: BaseException) -> ScrubbedError:
    return ScrubbedError(summary).with_traceback(original.__traceback__)


def describe_validation_error(error: ValidationError) -> str:
    """Summarize a ValidationError by model, location and error type.

    `str(error)` prints every `input_value`, `errors()` carries `input` and a
    `ctx` that can repeat it, and a validator's own message may quote it, so
    none of them reach a log line or Sentry.
    """
    locations = "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['type']}"
        for item in error.errors(
            include_url=False, include_context=False, include_input=False
        )
    )
    return f"{error.title}: {locations}"


def _scrubbed_link(error: BaseException) -> BaseException:
    if isinstance(error, ValidationError):
        # Pydantic chains nothing of the input, so the walk goes on below.
        replacement = scrubbed(describe_validation_error(error), error)
        replacement.__cause__ = error.__cause__
        replacement.__context__ = error.__context__
        replacement.__suppress_context__ = error.__suppress_context__
        return replacement
    if isinstance(error, DBAPIError):
        # Everything below is the driver and its adapter repeating the DETAIL
        # in their messages, so the replacement ends the chain.
        original = type(error.orig).__name__ if error.orig is not None else "unknown"
        return scrubbed(f"{type(error).__name__} from the driver ({original})", error)
    return error


def reportable(error: BaseException) -> BaseException:
    """The exception as it may appear in a log line or a Sentry event.

    A pydantic error prints the input it rejected; a driver error carries
    PostgreSQL's DETAIL with the row it refused. Both are replaced by a
    summary built from safe fields, keeping the traceback, wherever they are:
    logging and Sentry render the whole `__cause__` and `__context__` chain.
    The chain goes on below a pydantic error and ends at a driver error.
    The chain is rewritten in place, so `error` itself is safe afterwards too.
    """
    safe = _scrubbed_link(error)
    pending = [safe]
    seen: set[int] = set()
    while pending:
        link = pending.pop()
        if id(link) in seen:
            continue
        seen.add(id(link))
        # Assigning __cause__ also sets __suppress_context__.
        suppress_context = link.__suppress_context__
        if link.__cause__ is not None:
            link.__cause__ = _scrubbed_link(link.__cause__)
            pending.append(link.__cause__)
        if link.__context__ is not None:
            link.__context__ = _scrubbed_link(link.__context__)
            pending.append(link.__context__)
        link.__suppress_context__ = suppress_context
    return safe
