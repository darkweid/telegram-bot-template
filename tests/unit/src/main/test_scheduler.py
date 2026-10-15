from taskiq.schedule_sources import LabelScheduleSource

from src.core.tasks.broker import RetryScheduleSource
from src.core.tasks.heartbeat import HeartbeatScheduleSource
from src.main.config import Config
from src.main.scheduler import build_scheduler


def test_the_scheduler_fires_crons_retries_and_beats(settings: Config) -> None:
    """Without the retry source, delayed retries written by the worker would
    never fire; without the heartbeat source its healthcheck always fails."""
    scheduler = build_scheduler(settings)
    kinds = {type(source) for source in scheduler.sources}
    assert kinds == {LabelScheduleSource, RetryScheduleSource, HeartbeatScheduleSource}
