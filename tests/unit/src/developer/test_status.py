from src.developer.status import render_status
from src.system.schemas import HealthReport


def _report(**overrides: object) -> HealthReport:
    values: dict[str, object] = {
        "status": "ok",
        "version": "1.2.3",
        "mode": "polling",
        "postgres": True,
        "redis": True,
        "redis_memory_used_ratio": 0.125,
        "commit": "abc1234",
        "outbox_pending": 0,
        "outbox_oldest_pending_seconds": None,
        "task_queue_length": 4,
        "scheduler_heartbeat_age_seconds": 12.0,
    }
    return HealthReport(**{**values, **overrides})


def test_a_healthy_bot_reads_at_a_glance() -> None:
    assert render_status(_report(), "My <bot>") == "\n".join(
        [
            "<b>My &lt;bot&gt;</b> 1.2.3 (abc1234), polling",
            "Status: ok",
            "PostgreSQL: up",
            "Redis: up, memory 12.5%",
            "Task queue: 4",
            "Outbox: empty",
            "Scheduler: beat 12s ago",
        ]
    )


def test_a_broken_bot_says_what_is_unknown() -> None:
    """Every None in the report is an outage or a failed read; the developer
    must see "unknown", not a zero that looks healthy."""
    text = render_status(
        _report(
            status="degraded",
            postgres=False,
            redis_memory_used_ratio=None,
            outbox_pending=None,
            task_queue_length=None,
            scheduler_heartbeat_age_seconds=None,
        ),
        "Bot",
    )
    assert "Status: degraded" in text
    assert "PostgreSQL: down" in text
    assert "Redis: up, memory unknown" in text
    assert "Task queue: unknown" in text
    assert "Outbox: unknown" in text
    assert "Scheduler: no heartbeat" in text


def test_a_backlog_shows_its_age() -> None:
    text = render_status(
        _report(outbox_pending=7, outbox_oldest_pending_seconds=125.0), "Bot"
    )
    assert "Outbox: 7 pending, oldest 125s" in text
