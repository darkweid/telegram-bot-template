import json
import logging

from aiogram.exceptions import ClientDecodeError, TelegramBadRequest
from aiogram.methods import SendMessage
import pytest
import redis.exceptions as redis_exc

from src.core.errors.exceptions import CoreException
from src.core.errors.scrubbed import scrubbed
from src.core.limiter.limiter import RateLimiter
from src.core.redis.degradation import RedisDegradationReporter
from src.core.telegram import alerts as alerts_module
from src.core.telegram.alerts import Alert, DeveloperAlerter
from tests.fakes import redis as fake_redis_module
from tests.fakes.redis import InMemoryRedis
from tests.fakes.telegram import FakeTelegramSession, make_bot
from tests.helpers.logs import captured_logs


def _limiter(redis: InMemoryRedis | None = None) -> RateLimiter:
    return RateLimiter(
        redis or InMemoryRedis(),
        prefix="alerts",
        reporter=RedisDegradationReporter("Alerts"),
    )


def _alerter(
    session: FakeTelegramSession,
    recipients: tuple[int, ...] = (-100500,),
    limiter: RateLimiter | None = None,
) -> DeveloperAlerter:
    return DeveloperAlerter(
        make_bot(session),
        limiter or _limiter(),
        recipients=recipients,
        cooldown_seconds=300,
    )


def _failure() -> RuntimeError:
    try:
        raise RuntimeError("boom <b>")
    except RuntimeError as error:
        return error


def _user_input_failure(text: str) -> ValueError:
    try:
        int(text)
    except ValueError as error:
        return error
    raise AssertionError("expected a ValueError")


def _expected_failure() -> CoreException:
    try:
        raise CoreException("boom <b>")
    except CoreException as error:
        return error


def _other_failure() -> RuntimeError:
    try:
        raise RuntimeError("boom")
    except RuntimeError as error:
        return error


def _read_settings() -> object:
    return json.loads("{")


def _read_profile() -> object:
    return json.loads("[")


def _library_failure(read: object) -> json.JSONDecodeError:
    try:
        read()  # type: ignore[operator]
    except json.JSONDecodeError as error:
        return error
    raise AssertionError("expected a decode error")


def _compiled_failure() -> BaseException:
    # Raised from code with no file behind it, as exec, eval and frozen
    # modules have.
    namespace: dict[str, object] = {}
    exec(
        compile("def broken():\n    raise RuntimeError('x')\n", "<string>", "exec"),
        namespace,
    )  # nosec B102
    try:
        namespace["broken"]()  # type: ignore[operator]
    except RuntimeError as error:
        return error
    raise AssertionError("expected an error")


class ClosingLoop:
    """A loop that is shutting down and refuses new tasks."""

    def create_task(self, coroutine: object) -> None:
        raise RuntimeError("loop is closed")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_an_alert_names_the_error_its_origin_user_and_sentry_event() -> None:
    session = FakeTelegramSession()
    await _alerter(session).alert_error(
        Alert(_failure(), origin="update message", user_id=42, sentry_event_id="abc123")
    )
    [sent] = session.sent(SendMessage)
    assert sent.chat_id == -100500
    for part in ("RuntimeError", "update message", "_failure", "42", "abc123"):
        assert part in sent.text
    assert "message in the logs and Sentry" in sent.text


async def test_an_error_message_that_can_quote_user_input_stays_out() -> None:
    """`int(message.text)` puts what the user typed into the ValueError; the
    alert chat is no place for it, the logs and Sentry keep it."""
    session = FakeTelegramSession()
    await _alerter(session).alert_error(
        Alert(_user_input_failure("call me at +7 999 123"), origin="update message")
    )
    [sent] = session.sent(SendMessage)
    assert "ValueError" in sent.text
    assert "999" not in sent.text
    assert "message in the logs" in sent.text


async def test_a_project_exception_shows_its_message() -> None:
    """A CoreException that fails a task carries a message the project wrote."""
    session = FakeTelegramSession()
    await _alerter(session).alert_error(
        Alert(_expected_failure(), origin="task test.fail", task_id="task-1")
    )
    [sent] = session.sent(SendMessage)
    # The message is HTML: an error message is text, never markup.
    assert "<pre>boom &lt;b&gt;</pre>" in sent.text


async def test_a_scrubbed_error_shows_its_summary() -> None:
    """`reportable` rebuilt the summary from safe fields: it is what tells
    developers which driver error it was."""
    session = FakeTelegramSession()
    error = scrubbed("IntegrityError from the driver (UniqueViolation)", _failure())
    await _alerter(session).alert_error(Alert(error, origin="update message"))
    [sent] = session.sent(SendMessage)
    assert "IntegrityError from the driver (UniqueViolation)" in sent.text


async def test_every_recipient_gets_the_alert() -> None:
    """Without DEV_ALERT_CHAT_ID each developer is told in private."""
    session = FakeTelegramSession()
    await _alerter(session, recipients=(11, 22)).alert_error(
        Alert(_failure(), origin="update message")
    )
    assert [sent.chat_id for sent in session.sent(SendMessage)] == [11, 22]


async def test_a_repeated_failure_is_alerted_once_per_cooldown() -> None:
    """A broken handler fails every update; one alert per update would bury
    the alert chat while it lasts."""
    session = FakeTelegramSession()
    alerter = _alerter(session)
    for _ in range(5):
        await alerter.alert_error(Alert(_failure(), origin="update message"))
    assert len(session.sent(SendMessage)) == 1


async def test_a_failure_elsewhere_is_alerted_separately() -> None:
    session = FakeTelegramSession()
    alerter = _alerter(session)
    await alerter.alert_error(Alert(_failure(), origin="update message"))
    await alerter.alert_error(Alert(_other_failure(), origin="update message"))
    await alerter.alert_error(Alert(_failure(), origin="task outbox.sweep"))
    assert len(session.sent(SendMessage)) == 3


async def test_a_redelivered_task_is_not_alerted_again_after_the_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery withheld while another worker held the task is reclaimed
    after the stream's idle timeout, past the cooldown, and can fail once
    more: developers already know that task failed for good."""
    clock = Clock()
    monkeypatch.setattr(fake_redis_module, "_now", clock)
    session = FakeTelegramSession()
    alerter = _alerter(session)
    await alerter.alert_error(
        Alert(_failure(), origin="task test.fail", task_id="task-1")
    )
    clock.now += 600 + 300
    await alerter.alert_error(
        Alert(_failure(), origin="task test.fail", task_id="task-1")
    )
    await alerter.alert_error(
        Alert(_failure(), origin="task test.fail", task_id="task-2")
    )
    texts = [sent.text for sent in session.sent(SendMessage)]
    assert len(texts) == 2
    assert "task id task-1" in texts[0]
    assert "task id task-2" in texts[1]


async def test_the_cooldown_holds_while_redis_is_down() -> None:
    redis = InMemoryRedis()
    redis.fail_next_commands(100)
    session = FakeTelegramSession()
    alerter = _alerter(session, limiter=_limiter(redis))
    for _ in range(5):
        await alerter.alert_error(Alert(_failure(), origin="update message"))
    assert len(session.sent(SendMessage)) == 1


async def test_a_long_error_message_is_cut() -> None:
    """Telegram refuses a message over 4096 characters, and the alert with it."""
    session = FakeTelegramSession()
    await _alerter(session).alert_error(
        Alert(CoreException("x" * 10_000), origin="update message")
    )
    [sent] = session.sent(SendMessage)
    assert len(sent.text) < 1000


async def test_a_failed_delivery_is_only_logged() -> None:
    """An alert about a failed alert would loop."""
    session = FakeTelegramSession()
    session.queue(
        SendMessage,
        TelegramBadRequest(
            method=SendMessage(chat_id=1, text="x"), message="chat not found"
        ),
    )
    with captured_logs("src.core.telegram.alerts") as records:
        await _alerter(session, recipients=(11, 22)).alert_error(
            Alert(_failure(), origin="update message")
        )
    assert [sent.chat_id for sent in session.sent(SendMessage)] == [11, 22]
    assert any("TelegramBadRequest" in record.getMessage() for record in records)


async def test_a_degradation_notice_is_sent_in_the_background() -> None:
    """The degradation reporter calls notify() from synchronous code under a
    lock; the reporter already limits it to one per incident."""
    session = FakeTelegramSession()
    alerter = _alerter(session)
    alerter.notify("Redis unavailable for Cache")
    alerter.notify("Redis unavailable for Cache")
    await alerter.drain()
    assert [sent.text for sent in session.sent(SendMessage)] == [
        "Redis unavailable for Cache",
        "Redis unavailable for Cache",
    ]


def test_notify_outside_a_running_loop_is_only_logged() -> None:
    session = FakeTelegramSession()
    with captured_logs("src.core.telegram.alerts") as records:
        _alerter(session).notify("Redis unavailable for Cache")
    assert session.sent(SendMessage) == []
    [record] = records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "Alert not sent: no running event loop"


async def test_without_recipients_nothing_is_sent() -> None:
    """No alert chat and no developers: a fresh bot must still start and run,
    without a Redis round trip per failure for an alert nobody gets."""
    session = FakeTelegramSession()
    redis = InMemoryRedis()
    await _alerter(session, recipients=(), limiter=_limiter(redis)).alert_error(
        Alert(_failure(), origin="update message")
    )
    assert session.sent(SendMessage) == []
    assert redis.evalsha_keys == []
    assert redis.keys_snapshot() == []


async def test_an_undecodable_answer_is_only_logged() -> None:
    """The alert is sent from inside the failure it reports; an error of its
    own would replace that failure."""
    session = FakeTelegramSession()
    session.queue(
        SendMessage, ClientDecodeError("bad answer", ValueError("x"), "<html>")
    )
    with captured_logs("src.core.telegram.alerts") as records:
        await _alerter(session, recipients=(11, 22)).alert_error(
            Alert(_failure(), origin="update message")
        )
    assert [sent.chat_id for sent in session.sent(SendMessage)] == [11, 22]
    [record] = records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Alert not delivered to 11: ClientDecodeError"


async def test_a_limiter_bug_fails_no_alert() -> None:
    """A ResponseError is no outage, so the limiter does not fail open on it;
    the alert still must not raise into the failure it reports."""
    redis = InMemoryRedis()
    redis.fail_next_commands(
        100, error=redis_exc.ResponseError("WRONGTYPE secret-key-name")
    )
    session = FakeTelegramSession()
    with captured_logs("src.core.telegram.alerts") as records:
        await _alerter(session, limiter=_limiter(redis)).alert_error(
            Alert(_failure(), origin="update message")
        )
    assert session.sent(SendMessage) == []
    [record] = records
    # The class name only: a Redis error can name a key.
    assert record.getMessage() == "Alert not sent: ResponseError"


async def test_a_library_error_from_two_handlers_is_alerted_twice() -> None:
    """A Bot API refusal or a driver error is raised inside the library, the
    same frame whichever handler called it: placing it there would mute every
    broken handler but the first."""
    session = FakeTelegramSession()
    alerter = _alerter(session)
    await alerter.alert_error(
        Alert(_library_failure(_read_settings), origin="update message")
    )
    await alerter.alert_error(
        Alert(_library_failure(_read_profile), origin="update message")
    )
    texts = [sent.text for sent in session.sent(SendMessage)]
    assert len(texts) == 2
    # The innermost frame of ours, not the library's.
    assert "test_alerts.py:_read_settings" in texts[0]
    assert "test_alerts.py:_read_profile" in texts[1]


async def test_a_frame_without_a_file_is_not_placed_in_the_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the image the working directory is the project: a pseudo-filename
    resolved against it would pass for project code and hide the real place."""
    monkeypatch.chdir(alerts_module.PROJECT_ROOT)
    session = FakeTelegramSession()
    await _alerter(session).alert_error(
        Alert(_compiled_failure(), origin="update message")
    )
    [sent] = session.sent(SendMessage)
    assert "at test_alerts.py:_compiled_failure" in sent.text


def test_a_closing_loop_refusing_the_notice_is_only_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify() is called from fail-open paths, which must not fail over it."""
    monkeypatch.setattr(alerts_module.asyncio, "get_running_loop", ClosingLoop)
    session = FakeTelegramSession()
    alerter = _alerter(session)
    with captured_logs("src.core.telegram.alerts") as records:
        alerter.notify("Redis is degraded")
    [record] = records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Alert not sent: RuntimeError"
    assert session.sent(SendMessage) == []
    assert alerter._background == set()
