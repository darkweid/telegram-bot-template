from datetime import datetime, timedelta

from loggers import get_logger
from src.core.cache.interface import Cache
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.utils.datetime_utils import get_utc_now
from src.main.config import BotConfig
from src.user.cache import user_cache_key
from src.user.repositories import UserRepository
from src.user.schemas import TelegramProfile, UserProfile
from src.user.services import needs_write

logger = get_logger(__name__)


class TouchUserUseCase:
    """Load the user behind an update, storing or refreshing them on the way.

    The cached snapshot answers most updates without the database; a missing,
    stale or outdated snapshot is read from the database and cached again.

    On that path the row is read `FOR UPDATE` and the snapshot is cached before
    the transaction ends. Every use case that changes the user (a ban, a locale)
    takes the row lock with its UPDATE and drops the snapshot after its commit,
    so its drop always lands after this write. Cached after the lock is
    released, the snapshot of a row read just before a ban could be stored
    again after the ban dropped it, and the ban would go unnoticed until the
    snapshot expired. The cost is one Redis call made under a row lock: against
    a hung Redis about two socket timeouts (10 s by default), the one retry
    `create_redis_client` allows; the cache then fails open.

    `FOR UPDATE` rather than `FOR SHARE`: two updates of one user that both
    need a write would each hold a share lock and deadlock upgrading it; with
    `FOR UPDATE` the second waits and reads the row the first wrote.
    """

    def __init__(
        self,
        uow: SQLAlchemyUnitOfWork,
        users: UserRepository,
        cache: Cache,
        bot_config: BotConfig,
    ) -> None:
        self._uow = uow
        self._users = users
        self._cache = cache
        self._interval = timedelta(seconds=bot_config.USER_TOUCH_INTERVAL_SECONDS)
        self._ttl = bot_config.USER_CACHE_TTL_SECONDS

    async def execute(self, profile: TelegramProfile) -> UserProfile:
        now = get_utc_now()
        cached: UserProfile | None = await self._cache.get(
            user_cache_key(profile.id), model=UserProfile
        )
        if cached is not None and not needs_write(cached, profile, now, self._interval):
            return cached
        snapshot, created = await self._load_or_write(profile, now)
        if created:
            logger.info("New user %s", profile.id)
        return snapshot

    async def _load_or_write(
        self, profile: TelegramProfile, now: datetime
    ) -> tuple[UserProfile, bool]:
        key = user_cache_key(profile.id)
        async with self._uow as uow:
            stored = await self._users.get_single(
                uow.session, for_update=True, id=profile.id
            )
            snapshot = UserProfile.from_model(stored) if stored else None
            if snapshot is not None and not needs_write(
                snapshot, profile, now, self._interval
            ):
                await self._cache.set(key, snapshot, ttl=self._ttl)
                return snapshot, False
            upserted = await self._users.upsert_from_telegram(uow.session, profile, now)
            snapshot = UserProfile.from_model(upserted.user)
            await self._cache.set(key, snapshot, ttl=self._ttl)
            try:
                await uow.commit()
            except BaseException:
                # Cached before the commit, the snapshot describes a row that
                # was never written.
                await self._cache.delete(key)
                raise
        return snapshot, upserted.created
