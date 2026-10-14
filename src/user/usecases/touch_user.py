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
        key = user_cache_key(profile.id)
        cached: UserProfile | None = await self._cache.get(key, model=UserProfile)
        if cached is not None and not needs_write(cached, profile, now, self._interval):
            return cached
        snapshot, created = await self._load_or_write(profile, now)
        await self._cache.set(key, snapshot, ttl=self._ttl)
        if created:
            logger.info("New user %s", profile.id)
        return snapshot

    async def _load_or_write(
        self, profile: TelegramProfile, now: datetime
    ) -> tuple[UserProfile, bool]:
        async with self._uow as uow:
            stored = await self._users.get_single(uow.session, id=profile.id)
            snapshot = UserProfile.from_model(stored) if stored else None
            if snapshot is not None and not needs_write(
                snapshot, profile, now, self._interval
            ):
                return snapshot, False
            upserted = await self._users.upsert_from_telegram(uow.session, profile, now)
            await uow.commit()
        return UserProfile.from_model(upserted.user), upserted.created
