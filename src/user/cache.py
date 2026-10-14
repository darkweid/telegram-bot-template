from functools import partial

from src.core.cache.interface import Cache, CacheKey
from src.core.database.uow import SQLAlchemyUnitOfWork

USERS_CACHE_NAMESPACE = "users"

# The outbox's copy of the after-commit delete, for a change the bot must not
# forget across a Redis outage (a ban): the delete there fails open.
DROP_USER_SNAPSHOT = "user.drop_snapshot"


def user_cache_key(user_id: int) -> CacheKey:
    return CacheKey(USERS_CACHE_NAMESPACE, str(user_id))


def invalidate_user_after_commit(
    uow: SQLAlchemyUnitOfWork, cache: Cache, user_id: int
) -> None:
    """Drop the user's cached snapshot once the change is committed.

    Not before: a concurrent update would cache the old row again in between.
    """
    uow.add_after_commit_hook(partial(cache.delete, user_cache_key(user_id)))
