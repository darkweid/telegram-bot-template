from datetime import timedelta

from loggers import get_logger
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.utils.datetime_utils import get_utc_now
from src.main.config import BotConfig
from src.user.repositories import UserRepository
from src.user.schemas import TelegramProfile, UserProfile
from src.user.services import needs_write

logger = get_logger(__name__)


class TouchUserUseCase:
    """Load the user behind an update, storing or refreshing them on the way."""

    def __init__(
        self, uow: SQLAlchemyUnitOfWork, users: UserRepository, bot_config: BotConfig
    ) -> None:
        self._uow = uow
        self._users = users
        self._interval = timedelta(seconds=bot_config.USER_TOUCH_INTERVAL_SECONDS)

    async def execute(self, profile: TelegramProfile) -> UserProfile:
        now = get_utc_now()
        async with self._uow as uow:
            stored = await self._users.get_single(uow.session, id=profile.id)
            snapshot = UserProfile.from_model(stored) if stored else None
            if snapshot is not None and not needs_write(
                snapshot, profile, now, self._interval
            ):
                return snapshot
            upserted = await self._users.upsert_from_telegram(uow.session, profile, now)
            await uow.commit()
        if upserted.created:
            logger.info("New user %s", profile.id)
        return UserProfile.from_model(upserted.user)
