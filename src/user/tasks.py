from dishka import FromDishka

from src.core.cache.interface import Cache
from src.core.tasks.registry import TaskSpec
from src.user.cache import DROP_USER_SNAPSHOT, user_cache_key
from src.user.usecases.notify_admins import NotifyAdminsOfNewUserUseCase

NOTIFY_ADMINS_OF_NEW_USER = "user.notify_admins_of_new_user"


async def notify_admins_of_new_user(
    user_id: int, notify: FromDishka[NotifyAdminsOfNewUserUseCase]
) -> str:
    sent = await notify.execute(user_id)
    return f"Notified {sent} admins."


async def drop_user_snapshot(user_id: int, cache: FromDishka[Cache]) -> None:
    """Raises while Redis is out, so the retry tries again: the after-commit
    delete this backs up already failed open."""
    await cache.delete_or_raise(user_cache_key(user_id))


USER_TASKS: tuple[TaskSpec, ...] = (
    # Never run twice: a retry, or a delivery reclaimed from a worker that
    # stopped midway, would repeat the notice to every admin who already got it.
    TaskSpec(NOTIFY_ADMINS_OF_NEW_USER, notify_admins_of_new_user, at_most_once=True),
    # A second delete changes nothing, so retrying is safe.
    TaskSpec(DROP_USER_SNAPSHOT, drop_user_snapshot, retry_on_error=True),
)
