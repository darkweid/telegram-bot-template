from collections.abc import Iterator
from contextlib import contextmanager

from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.types import Update
from aiogram.types.update import UpdateTypeLookupError
import sentry_sdk

from src.core.update_context import UpdateContext


def update_context_from(update: Update) -> UpdateContext:
    """Describe an update by ids only, the way aiogram itself resolves them.

    aiogram's own resolver knows which field of each update type holds the
    user and the chat, so this stays right as Telegram adds update types.
    """
    try:
        update_type = update.event_type
    except UpdateTypeLookupError:
        # A type newer than the installed aiogram: still logged, never fatal.
        update_type = "unknown"
    resolved = UserContextMiddleware.resolve_event_context(event=update)
    return UpdateContext(
        update_id=update.update_id,
        update_type=update_type,
        user_id=resolved.user.id if resolved.user else None,
        chat_id=resolved.chat.id if resolved.chat else None,
    )


@contextmanager
def sentry_scope_for(context: UpdateContext) -> Iterator[None]:
    """Open a Sentry scope of the update's own, tagged with its ids.

    The scope is dropped on exit, so its tags never leak into the next update
    handled by the same task.
    """
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("update_type", context.update_type)
        scope.set_context(
            "update", {"update_id": context.update_id, "chat_id": context.chat_id}
        )
        if context.user_id is not None:
            scope.set_user({"id": str(context.user_id)})
        yield
