from pydantic import ValidationError


class ScrubbedError(Exception):
    """Stands in for an exception whose message echoes user data.

    A driver's IntegrityError carries the PostgreSQL DETAIL (`Key (email)=(...)`,
    `Failing row contains (...)`) and a pydantic ValidationError prints every
    `input_value`; both reach a log line or a Sentry event through `str(exc)`
    and through the exception chain. This one carries a summary the caller
    built from safe fields, the original traceback, and no chain.
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
