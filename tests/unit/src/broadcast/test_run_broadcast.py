from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import CopyMessage, EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.i18n import I18n
import pytest

from src.broadcast.callbacks import BroadcastStopCallback
from src.broadcast.enums import BroadcastStatus
from src.broadcast.models import Broadcast
from src.broadcast.repositories import BroadcastRepository
from src.broadcast.schemas import Checkpoint
from src.broadcast.services import STALE_AFTER
from src.broadcast.usecases import run_broadcast as run_module
from src.broadcast.usecases.run_broadcast import RunBroadcastUseCase
from src.core.limiter.pacer import Pacer
from src.core.telegram.session import MAX_RETRY_SLEEP_SECONDS
from src.main.config import Config
from src.user.repositories import UserRepository
from tests.factories.users import build_user_model
from tests.fakes.db import FakeSessionFactory
from tests.fakes.telegram import FakeTelegramSession, make_bot

BROADCAST_ID = UUID("01900000-0000-7000-8000-000000000001")
ADMIN = 1
SOURCE_MESSAGE_ID = 7
PROGRESS_MESSAGE_ID = 8
RECIPIENTS = [10, 20, 30]
CREATED_AT = datetime(2026, 1, 1, 12, tzinfo=UTC)
COPY = CopyMessage(chat_id=10, from_chat_id=ADMIN, message_id=SOURCE_MESSAGE_ID)
FINISHED = "Broadcast finished: 3 of 3\nSent: 3\nUnreachable: 0\nFailed: 0"


def _broadcast(**overrides: Any) -> Broadcast:
    values: dict[str, Any] = {
        "id": BROADCAST_ID,
        "admin_id": ADMIN,
        "source_chat_id": ADMIN,
        "source_message_id": SOURCE_MESSAGE_ID,
        "progress_message_id": PROGRESS_MESSAGE_ID,
        "status": BroadcastStatus.RUNNING,
        "total": len(RECIPIENTS),
        "sent": 0,
        "failed": 0,
        "unreachable": 0,
        "cursor_user_id": 0,
        "stop_requested": False,
        "created_at": CREATED_AT,
    }
    return Broadcast(**(values | overrides))


async def _recipients(
    session: object, *, after: int, limit: int, registered_by: datetime
) -> list[int]:
    assert registered_by == CREATED_AT
    return [user_id for user_id in RECIPIENTS if user_id > after][:limit]


@dataclass
class _Clock:
    """Stands in for the run's clock and sleep: a sleep moves the clock on at
    once and is logged, together with every checkpoint and how many copies
    had gone out by then."""

    telegram: FakeTelegramSession
    now: float = 0.0
    events: list[tuple[str, float]] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.events.append(("sleep", seconds))
        self.now += seconds

    def checkpointed(self, *args: object, **kwargs: object) -> None:
        self.events.append(("checkpoint", len(self.telegram.sent(CopyMessage))))


def _install_clock(
    monkeypatch: pytest.MonkeyPatch,
    telegram: FakeTelegramSession,
    broadcasts: AsyncMock,
    lease: bool | None,
) -> _Clock:
    """`lease` is what every checkpoint answers: False goes on, True asks to
    stop, None means the lease was lost."""
    clock = _Clock(telegram)
    monkeypatch.setattr(run_module, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(run_module, "asyncio", SimpleNamespace(sleep=clock.sleep))

    def save_checkpoint(*args: object, **kwargs: object) -> bool | None:
        clock.checkpointed()
        return lease

    broadcasts.save_checkpoint.side_effect = save_checkpoint
    return clock


def _broadcasts() -> AsyncMock:
    broadcasts = AsyncMock(spec=BroadcastRepository)
    broadcasts.claim_run.return_value = _broadcast()
    broadcasts.save_checkpoint.return_value = False
    broadcasts.finish.return_value = True
    return broadcasts


def _users(locale: str | None = None) -> AsyncMock:
    users = AsyncMock(spec=UserRepository)
    users.get_single.return_value = build_user_model(
        id=ADMIN, language_code="en", locale=locale
    )
    users.get_recipient_ids.side_effect = _recipients
    return users


def _run(
    settings: Config,
    i18n: I18n,
    telegram: FakeTelegramSession,
    broadcasts: AsyncMock,
    users: AsyncMock | None = None,
) -> RunBroadcastUseCase:
    return RunBroadcastUseCase(
        FakeSessionFactory(),
        broadcasts,
        users or _users(),
        make_bot(telegram),
        i18n,
        Pacer(10_000),
        settings.broadcast.model_copy(update={"BROADCAST_CHECKPOINT_EVERY": 2}),
        settings.bot,
    )


def _copied_to(telegram: FakeTelegramSession) -> list[int | str]:
    return [copy.chat_id for copy in telegram.sent(CopyMessage)]


def _finished_with(broadcasts: AsyncMock) -> tuple[Checkpoint, BroadcastStatus]:
    kwargs = broadcasts.finish.await_args.kwargs
    return kwargs["checkpoint"], kwargs["status"]


async def test_a_broadcast_reaches_every_recipient_and_reports(
    settings: Config, i18n: I18n
) -> None:
    telegram = FakeTelegramSession()
    broadcasts = _broadcasts()
    status = await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID)
    assert status == BroadcastStatus.COMPLETED
    assert [
        (copy.chat_id, copy.from_chat_id, copy.message_id)
        for copy in telegram.sent(CopyMessage)
    ] == [(10, ADMIN, 7), (20, ADMIN, 7), (30, ADMIN, 7)]
    assert _finished_with(broadcasts) == (
        Checkpoint(cursor_user_id=30, sent=3, failed=0, unreachable=0),
        BroadcastStatus.COMPLETED,
    )
    [report] = telegram.sent(SendMessage)
    assert (report.chat_id, report.text) == (ADMIN, FINISHED)


async def test_progress_is_checkpointed_after_every_batch(
    settings: Config, i18n: I18n
) -> None:
    """The checkpoint is where a restarted run continues; between two of them
    at most one batch is sent twice."""
    broadcasts = _broadcasts()
    await _run(settings, i18n, FakeTelegramSession(), broadcasts).execute(BROADCAST_ID)
    cursors = [
        call.kwargs["checkpoint"].cursor_user_id
        for call in broadcasts.save_checkpoint.await_args_list
    ]
    assert cursors == [20, 30]


async def test_a_slow_batch_still_renews_the_lease(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch slowed by flood control can outlast STALE_AFTER; without a
    checkpoint inside it the resume task would start a second run."""
    monkeypatch.setattr(run_module, "CHECKPOINT_INTERVAL_SECONDS", 0)
    broadcasts = _broadcasts()
    await _run(settings, i18n, FakeTelegramSession(), broadcasts).execute(BROADCAST_ID)
    cursors = [
        call.kwargs["checkpoint"].cursor_user_id
        for call in broadcasts.save_checkpoint.await_args_list
    ]
    assert cursors == [10, 20, 20, 30, 30]


async def test_a_stop_takes_effect_at_a_checkpoint_inside_a_batch(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch can take minutes under flood control; the admin's Stop must not
    wait for its end."""
    monkeypatch.setattr(run_module, "CHECKPOINT_INTERVAL_SECONDS", 0)
    telegram = FakeTelegramSession()
    broadcasts = _broadcasts()
    broadcasts.save_checkpoint.return_value = True
    status = await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID)
    assert status == BroadcastStatus.CANCELLED
    assert _copied_to(telegram) == [10]
    assert _finished_with(broadcasts)[0].cursor_user_id == 10


async def test_a_refused_recipient_is_counted_and_the_rest_still_get_it(
    settings: Config, i18n: I18n
) -> None:
    telegram = FakeTelegramSession()
    telegram.queue(
        CopyMessage,
        TelegramForbiddenError(
            method=COPY, message="Forbidden: bot was blocked by the user"
        ),
    )
    telegram.queue(
        CopyMessage,
        TelegramBadRequest(method=COPY, message="Bad Request: chat not found"),
    )
    broadcasts = _broadcasts()
    status = await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID)
    assert status == BroadcastStatus.COMPLETED
    assert _copied_to(telegram) == [10, 20, 30]
    assert _finished_with(broadcasts)[0] == Checkpoint(
        cursor_user_id=30, sent=1, failed=1, unreachable=1
    )


async def test_flood_control_is_waited_out_and_the_same_user_retried(
    settings: Config, i18n: I18n
) -> None:
    telegram = FakeTelegramSession()
    telegram.queue(
        CopyMessage,
        TelegramRetryAfter(
            method=COPY, message="Flood control exceeded", retry_after=0
        ),
    )
    broadcasts = _broadcasts()
    await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID)
    assert _copied_to(telegram) == [10, 10, 20, 30]
    assert _finished_with(broadcasts)[0].sent == 3


async def test_a_stop_cuts_a_long_flood_wait_short(
    settings: Config, i18n: I18n
) -> None:
    """Telegram may ask for an hour of silence; a Stop pressed meanwhile acts
    at the next lease renewal, and the user who met the flood is not counted."""
    telegram = FakeTelegramSession()
    telegram.queue(
        CopyMessage,
        TelegramRetryAfter(
            method=COPY, message="Flood control exceeded", retry_after=3600
        ),
    )
    broadcasts = _broadcasts()
    broadcasts.save_checkpoint.return_value = True
    status = await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID)
    assert status == BroadcastStatus.CANCELLED
    assert _copied_to(telegram) == [10]
    assert _finished_with(broadcasts)[0] == Checkpoint(
        cursor_user_id=0, sent=0, failed=0, unreachable=0
    )


async def test_a_network_blip_is_retried(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second of lost connectivity must not end a broadcast to the whole
    user base."""
    monkeypatch.setattr(run_module, "TRANSIENT_RETRY_DELAYS_SECONDS", (0, 0, 0, 0))
    telegram = FakeTelegramSession()
    telegram.queue(CopyMessage, TelegramNetworkError(method=COPY, message="timeout"))
    broadcasts = _broadcasts()
    status = await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID)
    assert status == BroadcastStatus.COMPLETED
    assert _copied_to(telegram) == [10, 10, 20, 30]


async def test_a_lasting_failure_marks_the_broadcast_failed_and_raises(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error leaves the task, so the task middleware alerts developers;
    the admin learns from the report that it stopped."""
    monkeypatch.setattr(run_module, "TRANSIENT_RETRY_DELAYS_SECONDS", (0, 0, 0, 0))
    telegram = FakeTelegramSession()
    for _ in range(5):
        telegram.queue(
            CopyMessage, TelegramNetworkError(method=COPY, message="timeout")
        )
    broadcasts = _broadcasts()
    with pytest.raises(TelegramNetworkError):
        await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID)
    assert _finished_with(broadcasts) == (
        Checkpoint(cursor_user_id=0, sent=0, failed=0, unreachable=0),
        BroadcastStatus.FAILED,
    )
    [report] = telegram.sent(SendMessage)
    assert report.text == "Broadcast failed: 0 of 3\nSent: 0\nUnreachable: 0\nFailed: 0"


async def test_a_deleted_source_message_fails_the_broadcast(
    settings: Config, i18n: I18n
) -> None:
    """Counted per recipient, a deleted original would walk the whole user
    base, finish as completed with every copy failed, and alert nobody."""
    telegram = FakeTelegramSession()
    telegram.queue(
        CopyMessage,
        TelegramBadRequest(
            method=COPY, message="Bad Request: message to copy not found"
        ),
    )
    broadcasts = _broadcasts()
    with pytest.raises(TelegramBadRequest):
        await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID)
    assert _copied_to(telegram) == [10]
    assert _finished_with(broadcasts)[1] == BroadcastStatus.FAILED


async def test_a_stop_ends_the_broadcast_at_the_checkpoint(
    settings: Config, i18n: I18n
) -> None:
    telegram = FakeTelegramSession()
    broadcasts = _broadcasts()
    broadcasts.save_checkpoint.return_value = True
    status = await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID)
    assert status == BroadcastStatus.CANCELLED
    assert _copied_to(telegram) == [10, 20]
    [report] = telegram.sent(SendMessage)
    assert (
        report.text == "Broadcast stopped: 2 of 3\nSent: 2\nUnreachable: 0\nFailed: 0"
    )


async def test_a_broadcast_stopped_before_its_start_sends_nothing(
    settings: Config, i18n: I18n
) -> None:
    telegram = FakeTelegramSession()
    broadcasts = _broadcasts()
    broadcasts.claim_run.return_value = _broadcast(stop_requested=True)
    status = await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID)
    assert status == BroadcastStatus.CANCELLED
    assert telegram.sent(CopyMessage) == []
    assert _finished_with(broadcasts)[1] == BroadcastStatus.CANCELLED


async def test_a_run_that_lost_its_lease_quits_without_a_trace(
    settings: Config, i18n: I18n
) -> None:
    """Another run owns the broadcast now; finishing or reporting from here
    would overwrite its counts and confuse the admin."""
    telegram = FakeTelegramSession()
    broadcasts = _broadcasts()
    broadcasts.save_checkpoint.return_value = None
    assert (
        await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID) is None
    )
    broadcasts.finish.assert_not_awaited()
    assert telegram.sent(SendMessage) == []


async def test_a_broadcast_owned_elsewhere_is_left_alone(
    settings: Config, i18n: I18n
) -> None:
    telegram = FakeTelegramSession()
    broadcasts = _broadcasts()
    broadcasts.claim_run.return_value = None
    assert (
        await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID) is None
    )
    assert telegram.requests == []


async def test_the_report_is_in_the_admins_language(
    settings: Config, i18n: I18n
) -> None:
    telegram = FakeTelegramSession()
    await _run(settings, i18n, telegram, _broadcasts(), _users(locale="ru")).execute(
        BROADCAST_ID
    )
    [report] = telegram.sent(SendMessage)
    assert (
        report.text
        == "Рассылка завершена: 3 из 3\nДоставлено: 3\nНедоступны: 0\nОшибки: 0"
    )


async def test_progress_is_shown_with_a_stop_button_then_replaced_by_the_report(
    settings: Config, i18n: I18n
) -> None:
    """Edits are rate limited to one per five seconds: the whole run here
    takes milliseconds, so only the first progress edit and the final one go
    out. The first goes before any copy, so Stop is there from the start."""
    telegram = FakeTelegramSession()
    await _run(settings, i18n, telegram, _broadcasts()).execute(BROADCAST_ID)
    first, last = telegram.sent(EditMessageText)
    first_copy = telegram.sent(CopyMessage)[0]
    assert telegram.requests.index(first) < telegram.requests.index(first_copy)
    assert (first.chat_id, first.message_id) == (ADMIN, PROGRESS_MESSAGE_ID)
    assert (
        first.text
        == "Broadcast in progress: 0 of 3\nSent: 0\nUnreachable: 0\nFailed: 0"
    )
    assert isinstance(first.reply_markup, InlineKeyboardMarkup)
    [[button]] = first.reply_markup.inline_keyboard
    assert (
        button.callback_data == BroadcastStopCallback(broadcast_id=BROADCAST_ID).pack()
    )
    assert (last.text, last.reply_markup) == (FINISHED, None)


async def test_a_long_streak_of_failed_copies_renews_the_lease(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retries of one recipient can run past CHECKPOINT_INTERVAL_SECONDS;
    without a renewal among them the lease could lapse mid-delivery."""
    monkeypatch.setattr(run_module, "CHECKPOINT_INTERVAL_SECONDS", 3)
    telegram = FakeTelegramSession()
    for _ in range(4):
        telegram.queue(
            CopyMessage, TelegramNetworkError(method=COPY, message="timeout")
        )
    broadcasts = _broadcasts()
    clock = _install_clock(monkeypatch, telegram, broadcasts, lease=False)
    status = await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID)
    assert status == BroadcastStatus.COMPLETED
    assert clock.events[:7] == [
        ("sleep", 1),
        ("sleep", 2),
        ("checkpoint", 2),
        ("sleep", 4),
        ("checkpoint", 3),
        ("sleep", 8),
        ("checkpoint", 4),
    ]
    assert _copied_to(telegram) == [10, 10, 10, 10, 10, 20, 30]


async def test_a_lease_lost_between_retries_stops_the_run_without_writing(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second run owns the broadcast; another attempt or a final write from
    here would send twice and overwrite its counts."""
    monkeypatch.setattr(run_module, "CHECKPOINT_INTERVAL_SECONDS", 3)
    telegram = FakeTelegramSession()
    for _ in range(4):
        telegram.queue(
            CopyMessage, TelegramNetworkError(method=COPY, message="timeout")
        )
    broadcasts = _broadcasts()
    clock = _install_clock(monkeypatch, telegram, broadcasts, lease=None)
    assert (
        await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID) is None
    )
    assert clock.events == [("sleep", 1), ("sleep", 2), ("checkpoint", 2)]
    assert _copied_to(telegram) == [10, 10]
    broadcasts.finish.assert_not_awaited()
    assert telegram.sent(SendMessage) == []


async def test_a_stop_between_retries_leaves_the_recipient_undone(
    settings: Config, i18n: I18n, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_module, "CHECKPOINT_INTERVAL_SECONDS", 3)
    telegram = FakeTelegramSession()
    for _ in range(4):
        telegram.queue(
            CopyMessage, TelegramNetworkError(method=COPY, message="timeout")
        )
    broadcasts = _broadcasts()
    _install_clock(monkeypatch, telegram, broadcasts, lease=True)
    status = await _run(settings, i18n, telegram, broadcasts).execute(BROADCAST_ID)
    assert status == BroadcastStatus.CANCELLED
    assert _copied_to(telegram) == [10, 10]
    assert _finished_with(broadcasts)[0] == Checkpoint(
        cursor_user_id=0, sent=0, failed=0, unreachable=0
    )


def test_the_longest_gap_between_checkpoints_stays_under_the_lease() -> None:
    """Past a due checkpoint a run can still make one copy and one progress
    edit, each a request that hangs to its timeout after the session's flood
    retries, and sleep one retry pause. The sum stays under half of
    STALE_AFTER, so a slow database or event loop still leaves room before a
    live run gets a twin."""
    one_call = run_module.TELEGRAM_REQUEST_TIMEOUT_SECONDS + MAX_RETRY_SLEEP_SECONDS
    longest_gap = (
        run_module.CHECKPOINT_INTERVAL_SECONDS
        + 2 * one_call
        + max(run_module.TRANSIENT_RETRY_DELAYS_SECONDS)
    )
    assert longest_gap < STALE_AFTER.total_seconds() / 2


async def test_every_call_of_a_run_is_capped_by_the_short_timeout(
    settings: Config, i18n: I18n
) -> None:
    """The session's 60 s default would let five hanging attempts of one copy
    outlast the lease on their own."""
    telegram = FakeTelegramSession()
    await _run(settings, i18n, telegram, _broadcasts()).execute(BROADCAST_ID)
    calls = [
        *telegram.sent(CopyMessage),
        *telegram.sent(EditMessageText),
        *telegram.sent(SendMessage),
    ]
    assert len(calls) == 6
    assert {telegram.timeout_of(call) for call in calls} == {
        run_module.TELEGRAM_REQUEST_TIMEOUT_SECONDS
    }
