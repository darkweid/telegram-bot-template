from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UpdateContext:
    """What the bot is handling right now, as far as logs and Sentry care.

    Ids only: no names, no message text. `user_id` and `chat_id` are None for
    updates that carry no user or chat (polls, some chat-boost updates).
    """

    update_id: int
    update_type: str
    user_id: int | None = None
    chat_id: int | None = None


# Set by UpdateContextMiddleware for the lifetime of one update and read by the
# logging filter, so every line emitted while handling it carries the same ids.
# None outside an update (startup, shutdown, background work).
_update_context: ContextVar[UpdateContext | None] = ContextVar(
    "update_context", default=None
)


def get_update_context() -> UpdateContext | None:
    """Return the update currently being handled, if any."""
    return _update_context.get()


@contextmanager
def bind_update_context(context: UpdateContext) -> Iterator[UpdateContext]:
    """Make `context` current for the block, restoring the previous one after.

    Polling and webhook both handle each update in its own task, and a task
    starts with a copy of its parent's context, so concurrent updates never
    see each other's value; the reset matters for code that handles several
    updates in one task, tests among it.
    """
    token = _update_context.set(context)
    try:
        yield context
    finally:
        _update_context.reset(token)
