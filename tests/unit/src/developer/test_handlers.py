from unittest.mock import AsyncMock

from aiogram.methods import SendMessage
from aiogram.utils.i18n import I18n
from dishka import Provider, Scope, provide

from src.main.config import Config
from src.system.schemas import HealthReport
from src.system.services import HealthService
from tests.factories.updates import build_message_update
from tests.fakes.telegram import FakeTelegramSession
from tests.helpers.dispatcher import running_bot
from tests.helpers.user import UseCases

DEVELOPER = 42
REPORT = HealthReport(
    status="ok",
    version="1.2.3",
    mode="polling",
    postgres=True,
    redis=True,
    redis_memory_used_ratio=None,
    commit="abc1234",
    outbox_pending=0,
    outbox_oldest_pending_seconds=None,
    task_queue_length=0,
    scheduler_heartbeat_age_seconds=3.0,
)


class HealthStub(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.service = AsyncMock(spec=HealthService)
        self.service.get_report.return_value = REPORT

    @provide(scope=Scope.REQUEST, override=True)
    def health(self) -> HealthService:
        return self.service


def _developer(settings: Config) -> Config:
    return settings.model_copy(
        update={
            "bot": settings.bot.model_copy(
                update={
                    "DEVELOPER_IDS": frozenset({DEVELOPER}),
                    "DEV_ALERT_CHAT_ID": None,
                }
            )
        }
    )


async def test_status_answers_a_developer(settings: Config, i18n: I18n) -> None:
    stub = HealthStub()
    async with running_bot(_developer(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/status", user_id=DEVELOPER))
        [reply] = running.session.sent(SendMessage)
    assert "abc1234" in reply.text
    stub.service.get_report.assert_awaited_once()


async def test_status_is_not_there_for_anyone_else(
    settings: Config, i18n: I18n
) -> None:
    """The report names versions and the state of every dependency; a user
    must not even learn the command exists."""
    stub = HealthStub()
    async with running_bot(_developer(settings), i18n, UseCases(), stub) as running:
        await running.feed(build_message_update("/status", user_id=77))
    stub.service.get_report.assert_not_awaited()


async def test_a_test_alert_goes_where_real_alerts_go(
    settings: Config, i18n: I18n
) -> None:
    """Without DEV_ALERT_CHAT_ID alerts go to each developer in private: the
    developer who asked gets the alert, through the alert Bot, and then the
    confirmation."""
    alerts = FakeTelegramSession()
    async with running_bot(
        _developer(settings), i18n, UseCases(), alert_session=alerts
    ) as running:
        await running.feed(build_message_update("/test_alert", user_id=DEVELOPER))
        [confirmation] = running.session.sent(SendMessage)
    [alert] = alerts.sent(SendMessage)
    assert alert.chat_id == DEVELOPER
    assert "Test alert" in alert.text
    assert confirmation.chat_id == DEVELOPER
    assert confirmation.text == "Test alert sent. Not there? The bot log says why."


async def test_a_repeated_test_alert_is_sent_again(
    settings: Config, i18n: I18n
) -> None:
    """The cooldown is for repeated failures; a developer checking delivery
    twice must see two alerts."""
    alerts = FakeTelegramSession()
    async with running_bot(
        _developer(settings), i18n, UseCases(), alert_session=alerts
    ) as running:
        for _ in range(2):
            await running.feed(build_message_update("/test_alert", user_id=DEVELOPER))
    sent = alerts.sent(SendMessage)
    assert sum("Test alert from" in message.text for message in sent) == 2
