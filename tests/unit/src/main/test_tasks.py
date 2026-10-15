from taskiq import InMemoryBroker
from taskiq.middlewares import SmartRetryMiddleware
from taskiq_redis import RedisStreamBroker

from src.broadcast.task_names import RESUME_STALLED_BROADCASTS, RUN_BROADCAST
from src.core.llm.tasks import purge_llm_usage
from src.core.tasks.middlewares import TaskFailureAlertMiddleware
from src.main import tasks as tasks_module
from src.main.config import Config
from src.main.event_subscribers import EVENT_SUBSCRIBERS
from src.main.tasks import TASKS, build_broker
from src.note.events import NoteCreated
from src.note.subscribers import enqueue_note_created
from src.note.tasks import NOTE_CREATED
from src.user.cache import DROP_USER_SNAPSHOT
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


def test_the_notice_never_runs_twice() -> None:
    """A retry, or a delivery reclaimed from a worker that stopped midway, would
    repeat the notice to every admin who already got it."""
    [spec] = [spec for spec in TASKS if spec.name == NOTIFY_ADMINS_OF_NEW_USER]
    assert spec.at_most_once is True
    assert spec.retry_on_error is False


def test_the_snapshot_drop_is_registered_and_retried() -> None:
    """Unregistered, the outbox would store a drop no worker runs; without a
    retry, one failed while Redis is out would never land."""
    [spec] = [spec for spec in TASKS if spec.name == DROP_USER_SNAPSHOT]
    assert spec.retry_on_error is True


def test_broadcasts_run_once_and_stalled_ones_resume_every_minute() -> None:
    """A retried run would restart a broadcast marked FAILED; without the
    resume task a run cut off by a restart would never finish."""
    specs = {spec.name: spec for spec in TASKS}
    assert specs[RUN_BROADCAST].retry_on_error is False
    assert specs[RUN_BROADCAST].schedule == ()
    assert specs[RESUME_STALLED_BROADCASTS].schedule == ({"cron": "* * * * *"},)


def test_the_note_subscriber_is_installed() -> None:
    """Without it a note is stored and its task never runs."""
    assert enqueue_note_created in EVENT_SUBSCRIBERS[NoteCreated]


def test_the_note_task_is_registered() -> None:
    assert NOTE_CREATED in {spec.name for spec in TASKS}


def test_llm_usage_is_purged_daily() -> None:
    """The purge keeps llm_usage at LLM_USAGE_RETENTION_DAYS; half an hour
    after the outbox purge, so the two never run at once."""
    [spec] = [spec for spec in TASKS if spec.name == "llm_usage.purge"]
    assert spec.func is purge_llm_usage
    assert spec.schedule == ({"cron": "30 3 * * *"},)
