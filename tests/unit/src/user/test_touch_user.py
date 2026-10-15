from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.core.cache.interface import Cache, CacheKey
from src.core.cache.memory_cache import InMemoryCache
from src.core.cache.serializer import JsonSerializer
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.events.registry import set_event_subscribers
from src.core.events.schemas import Actor, PublishedEvent
from src.main.config import Config
from src.user.cache import user_cache_key
from src.user.events import UserRegistered
from src.user.models import User
from src.user.repositories import UpsertedUser, UserRepository
from src.user.schemas import TelegramProfile, UserProfile
from src.user.usecases import touch_user as module
from src.user.usecases.touch_user import TouchUserUseCase
from tests.factories.users import build_cache, build_user_snapshot
from tests.fakes.db import FakeAsyncSession
from tests.helpers.logs import captured_logs

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
PROFILE = TelegramProfile(
    id=1, first_name="Ann", username="ann", last_name=None, language_code="en"
)


def _model(**overrides: object) -> User:
    fields: dict[str, object] = {
        "id": 1,
        "first_name": "Ann",
        "username": "ann",
        "last_name": None,
        "language_code": "en",
        "locale": None,
        "is_reachable": True,
        "is_banned": False,
        "last_seen_at": NOW,
    }
    return User(**(fields | overrides))


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "get_utc_now", lambda: NOW)


def _use_case(
    settings: Config, users: AsyncMock, cache: Cache | None = None
) -> tuple[TouchUserUseCase, FakeAsyncSession]:
    session = FakeAsyncSession()
    uow = SQLAlchemyUnitOfWork(session)
    return TouchUserUseCase(uow, users, cache or build_cache(), settings.bot), session


async def test_a_fresh_user_is_returned_without_a_write(settings: Config) -> None:
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = _model(last_seen_at=NOW - timedelta(seconds=10))
    use_case, session = _use_case(settings, users)
    profile = await use_case.execute(PROFILE)
    assert profile == UserProfile.from_model(users.get_single.return_value)
    users.upsert_from_telegram.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_a_new_user_is_written_and_committed(settings: Config) -> None:
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = None
    users.upsert_from_telegram.return_value = UpsertedUser(user=_model(), created=True)
    use_case, session = _use_case(settings, users)
    profile = await use_case.execute(PROFILE)
    assert profile.id == 1
    users.upsert_from_telegram.assert_awaited_once_with(session, PROFILE, NOW)
    session.commit.assert_awaited_once()


@pytest.mark.parametrize(
    "stored",
    [
        _model(last_seen_at=NOW - timedelta(hours=1)),
        _model(username="old"),
        _model(is_reachable=False, last_seen_at=NOW - timedelta(seconds=10)),
    ],
    ids=["stale-last-seen", "changed-profile", "unreachable-user-returns"],
)
async def test_an_existing_user_that_needs_a_write_is_upserted(
    settings: Config, stored: User
) -> None:
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = stored
    refreshed = _model(locale="ru")
    users.upsert_from_telegram.return_value = UpsertedUser(
        user=refreshed, created=False
    )
    use_case, session = _use_case(settings, users)
    profile = await use_case.execute(PROFILE)
    users.upsert_from_telegram.assert_awaited_once_with(session, PROFILE, NOW)
    session.commit.assert_awaited_once()
    assert profile == UserProfile.from_model(refreshed)


@pytest.mark.parametrize("created", [True, False])
async def test_only_a_new_user_is_logged(settings: Config, created: bool) -> None:
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = None
    users.upsert_from_telegram.return_value = UpsertedUser(
        user=_model(), created=created
    )
    use_case, _ = _use_case(settings, users)
    with captured_logs(module.__name__) as records:
        await use_case.execute(PROFILE)
    messages = [record.getMessage() for record in records]
    assert messages == (["New user 1"] if created else [])


async def test_a_fresh_cached_snapshot_skips_the_database(settings: Config) -> None:
    """Every update of every user passes here; the database is read only when
    the snapshot is missing, stale or out of date with Telegram."""
    cache = build_cache()
    snapshot = build_user_snapshot(last_seen_at=NOW - timedelta(seconds=10))
    await cache.set(user_cache_key(PROFILE.id), snapshot)
    users = AsyncMock(spec=UserRepository)
    use_case, _ = _use_case(settings, users, cache)
    assert await use_case.execute(PROFILE) == snapshot
    users.get_single.assert_not_awaited()


async def test_a_changed_telegram_profile_goes_to_the_database(
    settings: Config,
) -> None:
    """A cached snapshot must not hide a profile edit from the stored row."""
    cache = build_cache()
    await cache.set(
        user_cache_key(PROFILE.id),
        build_user_snapshot(last_seen_at=NOW - timedelta(seconds=10)),
    )
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = _model(last_seen_at=NOW - timedelta(seconds=10))
    users.upsert_from_telegram.return_value = UpsertedUser(
        user=_model(first_name="Bob"), created=False
    )
    use_case, _ = _use_case(settings, users, cache)
    result = await use_case.execute(replace(PROFILE, first_name="Bob"))
    assert result.first_name == "Bob"
    cached = await cache.get(user_cache_key(PROFILE.id), model=UserProfile)
    assert cached is not None and cached.first_name == "Bob"


async def test_a_database_read_fills_the_cache(settings: Config) -> None:
    """Without it every update of a user the cache never saw reads the row."""
    cache = build_cache()
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = _model(last_seen_at=NOW - timedelta(seconds=10))
    use_case, _ = _use_case(settings, users, cache)
    await use_case.execute(PROFILE)
    assert await cache.get(user_cache_key(PROFILE.id), model=UserProfile) is not None
    users.upsert_from_telegram.assert_not_awaited()


@pytest.mark.parametrize(
    "cached",
    [
        build_user_snapshot(last_seen_at=NOW - timedelta(hours=1)),
        build_user_snapshot(
            is_reachable=False, last_seen_at=NOW - timedelta(seconds=10)
        ),
    ],
    ids=["stale-last-seen", "unreachable-user-returns"],
)
async def test_a_cached_snapshot_that_needs_a_write_is_upserted(
    settings: Config, cached: UserProfile
) -> None:
    """A user who blocked the bot and came back must be marked reachable even
    though the cache still holds the old snapshot."""
    cache = build_cache()
    await cache.set(user_cache_key(PROFILE.id), cached)
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = _model(
        is_reachable=cached.is_reachable, last_seen_at=cached.last_seen_at
    )
    refreshed = _model(locale="ru")
    users.upsert_from_telegram.return_value = UpsertedUser(
        user=refreshed, created=False
    )
    use_case, session = _use_case(settings, users, cache)
    await use_case.execute(PROFILE)
    users.upsert_from_telegram.assert_awaited_once_with(session, PROFILE, NOW)
    assert await cache.get(
        user_cache_key(PROFILE.id), model=UserProfile
    ) == UserProfile.from_model(refreshed)


class TransactionWatchingCache(InMemoryCache):
    """Records, at every write, whether the session's transaction was still open."""

    def __init__(self) -> None:
        super().__init__(JsonSerializer(), default_ttl=60, version_ttl=600)
        self.session: FakeAsyncSession | None = None
        self.writes_in_transaction: list[bool] = []

    async def set(self, key: CacheKey, value: Any, *, ttl: int | None = None) -> None:
        assert self.session is not None
        ended = self.session.commit.await_count + self.session.rollback.await_count
        self.writes_in_transaction.append(ended == 0)
        await super().set(key, value, ttl=ttl)


@pytest.mark.parametrize("write", [False, True], ids=["read-only", "upsert"])
async def test_the_snapshot_is_cached_while_the_row_is_locked(
    settings: Config, write: bool
) -> None:
    """A ban commits and drops the snapshot after it. Cached once the lock is
    released, the touch could store the unbanned row after that drop, and the
    ban would be ignored until the snapshot expires."""
    cache = TransactionWatchingCache()
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = _model(
        last_seen_at=NOW - (timedelta(hours=1) if write else timedelta(seconds=10))
    )
    users.upsert_from_telegram.return_value = UpsertedUser(user=_model(), created=False)
    use_case, session = _use_case(settings, users, cache)
    cache.session = session
    await use_case.execute(PROFILE)
    assert users.get_single.await_args is not None
    assert users.get_single.await_args.kwargs["for_update"] is True
    assert cache.writes_in_transaction == [True]


async def test_a_failed_commit_drops_the_snapshot_it_cached(settings: Config) -> None:
    """The snapshot was cached before the commit; kept, it would describe a row
    that was never written."""
    cache = build_cache()
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = None
    users.upsert_from_telegram.return_value = UpsertedUser(user=_model(), created=True)
    use_case, session = _use_case(settings, users, cache)
    session.commit.side_effect = ConnectionError("database down")
    with pytest.raises(ConnectionError):
        await use_case.execute(PROFILE)
    assert await cache.get(user_cache_key(PROFILE.id), model=UserProfile) is None


@pytest.fixture
def published() -> list[PublishedEvent]:
    """Record what the use case publishes; the root conftest restores the
    registry."""
    seen: list[PublishedEvent] = []

    async def record(_uow: SQLAlchemyUnitOfWork, event: PublishedEvent) -> None:
        seen.append(event)

    set_event_subscribers({UserRegistered: [record]})
    return seen


async def test_a_new_user_publishes_user_registered(
    settings: Config, published: list[PublishedEvent]
) -> None:
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = None
    users.upsert_from_telegram.return_value = UpsertedUser(user=_model(), created=True)
    use_case, _session = _use_case(settings, users)
    await use_case.execute(PROFILE)
    [event] = published
    assert event.event == UserRegistered(user_id=PROFILE.id)
    assert event.actor == Actor.telegram_user(PROFILE.id)


async def test_a_refreshed_user_publishes_nothing(
    settings: Config, published: list[PublishedEvent]
) -> None:
    """A user who returns after an hour is not new; admins would get a
    notice for every comeback."""
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = _model(last_seen_at=NOW - timedelta(hours=1))
    users.upsert_from_telegram.return_value = UpsertedUser(user=_model(), created=False)
    use_case, _session = _use_case(settings, users)
    await use_case.execute(PROFILE)
    assert published == []


async def test_a_failed_subscriber_leaves_nothing_cached_or_committed(
    settings: Config,
) -> None:
    """The registration and its outbox row commit together or not at all; a
    snapshot cached anyway would describe a user who was never stored."""

    async def broken(_uow: SQLAlchemyUnitOfWork, _event: PublishedEvent) -> None:
        raise ConnectionError("outbox insert failed")

    set_event_subscribers({UserRegistered: [broken]})
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = None
    users.upsert_from_telegram.return_value = UpsertedUser(user=_model(), created=True)
    cache = build_cache()
    use_case, session = _use_case(settings, users, cache)
    with pytest.raises(ConnectionError):
        await use_case.execute(PROFILE)
    session.commit.assert_not_awaited()
    assert await cache.get(user_cache_key(PROFILE.id), model=UserProfile) is None
