from taskiq import InMemoryBroker
from taskiq_redis import RedisStreamBroker

from src.main import tasks as tasks_module
from src.main.config import Config
from src.main.tasks import TASKS, build_broker


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
