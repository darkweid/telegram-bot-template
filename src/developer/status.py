"""/status text. English on purpose: developer output is not translated."""

from aiogram import html

from src.system.schemas import HealthReport


def render_status(report: HealthReport, project_name: str) -> str:
    """One line per dependency. A missing reading shows as "unknown", never as
    a zero: it means the dependency is down or did not answer in time."""
    if report.redis_memory_used_ratio is None:
        memory = "unknown"
    else:
        memory = f"{report.redis_memory_used_ratio:.1%}"
    lines = [
        f"<b>{html.quote(project_name)}</b> {html.quote(report.version)} "
        f"({html.quote(report.commit)}), {report.mode}",
        f"Status: {report.status}",
        f"PostgreSQL: {'up' if report.postgres else 'down'}",
        f"Redis: {'up' if report.redis else 'down'}, memory {memory}",
        f"Task queue: {_or_unknown(report.task_queue_length)}",
        f"Outbox: {_outbox(report)}",
        f"Scheduler: {_scheduler(report.scheduler_heartbeat_age_seconds)}",
    ]
    return "\n".join(lines)


def _or_unknown(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _outbox(report: HealthReport) -> str:
    if report.outbox_pending is None:
        return "unknown"
    if report.outbox_pending == 0:
        return "empty"
    text = f"{report.outbox_pending} pending"
    if report.outbox_oldest_pending_seconds is not None:
        text += f", oldest {report.outbox_oldest_pending_seconds:.0f}s"
    return text


def _scheduler(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "no heartbeat"
    return f"beat {age_seconds:.0f}s ago"
