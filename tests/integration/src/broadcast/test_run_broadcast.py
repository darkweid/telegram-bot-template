import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from aiogram.methods import CopyMessage
from aiogram.types import MessageId
from aiogram.utils.i18n import I18n
import pytest
import pytest_asyncio
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.broadcast.enums import BroadcastStatus
from src.broadcast.models import Broadcast
from src.broadcast.repositories import BroadcastRepository
from src.broadcast.usecases import run_broadcast as run_module
from src.broadcast.usecases.run_broadcast import RunBroadcastUseCase
from src.core.limiter.pacer import Pacer
from src.main.config import Config
from src.user.models import User
from src.user.repositories import UserRepository
from tests.fakes.telegram import FakeTelegramSession, make_bot
from tests.integration.conftest import build_user_data

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Above every id new_user_id() hands out (10**9 + below 2**40), so the walk
# meets only this module's users.
BASE = 2**60
RECIPIENTS = 120
EVERY = 50
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)

Sessions = async_sessionmaker[AsyncSession]


async def _clean(sessions: Sessions) -> None:
    async with sessions() as session:
        await session.execute(delete(Broadcast).where(Broadcast.admin_id == BASE))
        await session.execute(delete(User).where(User.id >= BASE))
        await session.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def sessions(integration_engine: AsyncEngine) -> AsyncGenerator[Sessions]:
    """Committed rows, unlike db_session: the use case commits through
    sessions of its own. Cleaned before and after, so a crashed test leaves
    nothing for the next one."""
    sessionmaker = async_sessionmaker(integration_engine, expire_on_commit=False)
    await _clean(sessionmaker)
    yield sessionmaker
    await _clean(sessionmaker)


async def _seed(sessions: Sessions, **broadcast: Any) -> UUID:
    async with sessions() as session:
        session.add_all(
            User(**build_user_data(BASE + offset, language_code="en"))
            for offset in range(RECIPIENTS + 1)
        )
        # Flushed first: without a relationship the unit of work does not
        # order the inserts by the foreign key. One transaction all the same,
        # so every row shares its now() as created_at.
        await session.flush()
        row = Broadcast(
            admin_id=BASE,
            source_chat_id=BASE,
            source_message_id=7,
            progress_message_id=8,
            total=RECIPIENTS,
            cursor_user_id=BASE,
            **broadcast,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _load(sessions: Sessions, broadcast_id: UUID) -> Broadcast:
    async with sessions() as session:
        row = await session.get(Broadcast, broadcast_id)
    assert row is not None
    return row


def _run(
    sessions: Sessions, telegram: FakeTelegramSession, settings: Config, i18n: I18n
) -> RunBroadcastUseCase:
    return RunBroadcastUseCase(
        sessions,
        BroadcastRepository(),
        UserRepository(),
        make_bot(telegram),
        i18n,
        Pacer(10_000),
        settings.broadcast.model_copy(update={"BROADCAST_CHECKPOINT_EVERY": EVERY}),
        settings.bot,
    )


async def test_a_restarted_broadcast_resumes_from_the_checkpoint(
    sessions: Sessions, settings: Config, i18n: I18n
) -> None:
    """A worker restart mid-batch: the next run starts after the last
    checkpoint, so nobody is skipped and at most BROADCAST_CHECKPOINT_EVERY
    users get the message twice."""
    broadcast_id = await _seed(sessions)
    first = FakeTelegramSession()
    for _ in range(EVERY + 19):
        first.queue(CopyMessage, MessageId(message_id=1))
    # The 70th copy is where the worker stops.
    first.queue(CopyMessage, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _run(sessions, first, settings, i18n).execute(broadcast_id)

    cut_off = await _load(sessions, broadcast_id)
    assert (cut_off.status, cut_off.cursor_user_id, cut_off.sent) == (
        BroadcastStatus.RUNNING,
        BASE + EVERY,
        EVERY,
    )
    # The dead run's lease runs out.
    async with sessions() as session:
        await session.execute(
            update(Broadcast)
            .where(Broadcast.id == broadcast_id)
            .values(checkpoint_at=Broadcast.checkpoint_at - timedelta(hours=1))
        )
        await session.commit()

    second = FakeTelegramSession()
    status = await _run(sessions, second, settings, i18n).execute(broadcast_id)
    assert status == BroadcastStatus.COMPLETED
    first_ids = {copy.chat_id for copy in first.sent(CopyMessage)}
    second_ids = {copy.chat_id for copy in second.sent(CopyMessage)}
    assert first_ids | second_ids == {BASE + n for n in range(1, RECIPIENTS + 1)}
    assert len(first_ids & second_ids) <= EVERY
    done = await _load(sessions, broadcast_id)
    assert (done.status, done.sent, done.cursor_user_id) == (
        BroadcastStatus.COMPLETED,
        RECIPIENTS,
        BASE + RECIPIENTS,
    )


async def test_a_live_run_is_not_joined_by_a_second(
    sessions: Sessions,
    settings: Config,
    i18n: I18n,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resume task may queue a run for a broadcast that is only slow;
    two runs would send every message twice."""
    broadcast_id = await _seed(
        sessions, status=BroadcastStatus.RUNNING, run_id=uuid4(), checkpoint_at=NOW
    )
    monkeypatch.setattr(run_module, "get_utc_now", lambda: NOW + timedelta(minutes=1))
    telegram = FakeTelegramSession()
    assert await _run(sessions, telegram, settings, i18n).execute(broadcast_id) is None
    assert telegram.sent(CopyMessage) == []
