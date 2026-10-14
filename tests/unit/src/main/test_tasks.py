from taskiq import InMemoryBroker
from taskiq.middlewares import SmartRetryMiddleware
from taskiq_redis import RedisStreamBroker

from src.core.tasks.middlewares import TaskFailureAlertMiddleware
from src.main import tasks as tasks_module
from src.main.config import Config
from src.main.event_subscribers import EVENT_SUBSCRIBERS
from src.main.tasks import TASKS, build_broker
from src.user.events import UserRegistered
from src.user.subscribers import enqueue_new_user_notice
from src.user.tasks import NOTIFY_ADMINS_OF_NEW_USER


def test_tests_get_the_in_memory_broker(settings: Config) -> None:
    assert isinstance(build_broker(settings), InMemoryBroker)


def test_a_deploy_gets_the_stream_broker_with_every_task(settings: Config) -> None:
    deployed = settings.model_copy(
        update={"app": settings.app.model_copy(update={"TESTING": False})}
    )
    broker = build_broker(deployed)
    assert isinstance(broker, RedisStreamBroker)
    assert set(broker.get_all_tasks()) == {spec.name for spec in TASKS}


def test_task_names_are_unique() -> None:
    """A second registration under one name silently replaces the first."""
    names = [spec.name for spec in tasks_module.TASKS]
    assert len(names) == len(set(names))


def test_the_outbox_is_swept_every_minute_and_purged_daily() -> None:
    """Without the sweep a failed after-commit publish is never sent; without
    the purge the table grows forever."""
    schedules = {spec.name: spec.schedule for spec in TASKS}
    assert schedules["outbox.sweep"] == ({"cron": "* * * * *"},)
    assert schedules["outbox.purge"] == ({"cron": "0 3 * * *"},)


def test_the_failure_alert_runs_after_the_retry_decision(settings: Config) -> None:
    """taskiq runs on_error in reverse: first in the list is last to decide."""
    deployed = settings.model_copy(
        update={"app": settings.app.model_copy(update={"TESTING": False})}
    )
    broker = build_broker(deployed)
    kinds = [type(middleware) for middleware in broker.middlewares]
    assert kinds.index(TaskFailureAlertMiddleware) < kinds.index(SmartRetryMiddleware)


def test_the_test_broker_alerts_on_a_failed_task(settings: Config) -> None:
    kinds = [type(middleware) for middleware in build_broker(settings).middlewares]
    assert TaskFailureAlertMiddleware in kinds


def test_the_registration_subscriber_is_installed() -> None:
    """Without it a new user is stored and nobody is told."""
    assert enqueue_new_user_notice in EVENT_SUBSCRIBERS[UserRegistered]


def test_the_notice_task_is_registered() -> None:
    """The outbox would store the notice under a name no worker runs."""
    assert NOTIFY_ADMINS_OF_NEW_USER in {spec.name for spec in TASKS}


def test_the_notice_is_never_retried() -> None:
    """A retry would repeat the notice to every admin who already got it."""
    [spec] = [spec for spec in TASKS if spec.name == NOTIFY_ADMINS_OF_NEW_USER]
    assert spec.retry_on_error is False
