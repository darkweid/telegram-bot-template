from pathlib import Path
import re
from typing import Any

import yaml

from src.core.tasks.heartbeat import HEARTBEAT_INTERVAL_SECONDS
from src.main.config import BotConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load(relative_path: str) -> dict[str, Any]:
    text = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def _services() -> dict[str, Any]:
    services: dict[str, Any] = _load("infra/docker-compose.yml")["services"]
    return services


def _seconds(duration: str) -> int:
    match = re.fullmatch(r"(\d+)s", duration)
    assert match, f"write durations in seconds, got {duration!r}"
    return int(match.group(1))


def test_docker_waits_longer_than_the_bot_does_on_shutdown() -> None:
    """Past stop_grace_period Docker kills the process, and every update still
    being handled is lost; the bot's own timeout must expire first."""
    bot = _services()["bot"]
    default_timeout = BotConfig.model_fields["SHUTDOWN_TIMEOUT_SECONDS"].default
    assert _seconds(bot["stop_grace_period"]) >= default_timeout + 5


def test_the_bot_publishes_no_port() -> None:
    """Health is for the compose network; nothing on the host or outside needs it."""
    assert "ports" not in _services()["bot"]


def test_the_healthcheck_probes_liveness_only() -> None:
    """/ready would make Docker restart a healthy bot whenever Postgres blips."""
    test = " ".join(_services()["bot"]["healthcheck"]["test"])
    assert "/live" in test
    assert "${WEB_SERVER_PORT:-8080}" in test


def test_the_builder_only_builds_the_image() -> None:
    """Started as a dependency, it must exit at once instead of running a second bot."""
    assert _services()["bot-builder"]["command"] == ["true"]


def test_every_service_has_resource_limits() -> None:
    for name, service in _services().items():
        if name == "bot-builder":
            continue  # builds the image and exits
        limits = service["deploy"]["resources"]["limits"]
        assert limits.get("cpus") and limits.get("memory"), name


def test_backing_services_listen_on_loopback_only() -> None:
    for name in ("postgres", "redis"):
        for mapping in _services()[name].get("ports", []):
            assert mapping.startswith("127.0.0.1:"), (name, mapping)


def test_redis_gets_its_own_password_and_no_other_secret() -> None:
    """The bot's env_file carries the bot token and every other secret; Redis
    needs only its password."""
    redis = _services()["redis"]
    assert "env_file" not in redis
    assert set(redis["environment"]) == {"REDIS_PASSWORD", "REDISCLI_AUTH"}
    assert " -a " not in " ".join(redis["healthcheck"]["test"])


def test_redis_password_stays_off_the_command_line() -> None:
    """An argument shows in `ps` and breaks on a password with spaces or
    quotes; the entrypoint script passes it on stdin."""
    redis = _services()["redis"]
    assert "REDIS_PASSWORD" not in " ".join(redis["command"])
    assert redis["environment"]["REDIS_PASSWORD"].startswith("${REDIS_PASSWORD:?")


def test_make_run_knows_the_local_bot_image_name() -> None:
    """`make run` builds only when the resolved bot image is the local default;
    a name that drifted from compose would pull an image no registry has."""
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^LOCAL_BOT_IMAGE = (\S+)$", makefile, re.MULTILINE)
    assert match
    for service in ("bot-builder", "migrate", "bot", "worker", "scheduler"):
        image = _services()[service]["image"]
        assert image == f"${{BOT_IMAGE:-{match.group(1)}}}"


def test_the_bot_waits_for_the_migrations() -> None:
    services = _services()
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["migrate"]["image"] == services["bot"]["image"]
    assert services["bot"]["depends_on"]["migrate"] == {
        "condition": "service_completed_successfully"
    }
    assert services["migrate"]["depends_on"]["postgres"] == {
        "condition": "service_healthy"
    }


def test_dev_override_polls_and_never_restarts() -> None:
    override = _load("infra/docker-compose.override.yml")["services"]
    assert override["bot"]["environment"]["BOT_MODE"] == "polling"
    for name in ("bot", "worker", "scheduler", "postgres", "redis"):
        assert override[name]["restart"] == "no", name


def test_dev_override_keeps_the_production_image_out_of_the_dev_stack() -> None:
    """The dev bot migrates itself from the mounted tree; the one-shot service
    must neither pull BOT_IMAGE nor run the baked-in migrations."""
    migrate = _load("infra/docker-compose.override.yml")["services"]["migrate"]
    assert migrate["image"] == "tg-bot-template-bot-dev:latest"
    assert migrate["command"] == ["true"]


def test_the_worker_waits_for_the_migrations() -> None:
    """Tasks read and write the same tables the bot does."""
    depends = _services()["worker"]["depends_on"]
    assert depends["migrate"] == {"condition": "service_completed_successfully"}
    assert depends["redis"] == {"condition": "service_healthy"}


def test_the_scheduler_needs_only_redis() -> None:
    """It never opens the database, so a failed migration must not stop
    the schedule."""
    depends = _services()["scheduler"]["depends_on"]
    assert "migrate" not in depends
    assert depends["redis"] == {"condition": "service_healthy"}


def test_the_dev_worker_waits_for_the_bot_to_migrate() -> None:
    """The dev `migrate` exits without migrating; the dev bot does it before
    its healthcheck turns green."""
    worker = _load("infra/docker-compose.override.yml")["services"]["worker"]
    assert worker["depends_on"]["bot"] == {"condition": "service_healthy"}
    assert "--wait-tasks-timeout 25" in worker["command"]


def test_the_worker_outlasts_its_own_task_timeout() -> None:
    command = _services()["worker"]["command"]
    wait = int(command[command.index("--wait-tasks-timeout") + 1])
    assert _seconds(_services()["worker"]["stop_grace_period"]) >= wait + 5


def test_the_worker_runs_one_process_with_the_idempotent_receiver() -> None:
    """Two processes would double the database pool per container."""
    command = _services()["worker"]["command"]
    assert command[command.index("--workers") + 1] == "1"
    assert command[command.index("--receiver") + 1] == (
        "src.core.tasks.receiver:IdempotencyReceiver"
    )


def test_the_scheduler_beats_as_often_as_its_heartbeat_expects() -> None:
    """The heartbeat is written once per schedule update; a longer interval
    would let the healthcheck kill a healthy scheduler."""
    scheduler = _services()["scheduler"]
    command = scheduler["command"]
    interval = int(command[command.index("--update-interval") + 1])
    assert interval == HEARTBEAT_INTERVAL_SECONDS
    assert scheduler["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "src.main.heartbeat",
    ]


def test_the_makefile_pulls_every_service_of_the_bot_image() -> None:
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "pull migrate bot worker scheduler" in makefile


def test_the_image_build_receives_the_commit() -> None:
    """/status and /health name the commit; the value exists only at build
    time, since .git never enters the image."""
    args = _services()["bot-builder"]["build"]["args"]
    assert args["COMMIT_SHA"] == "${COMMIT_SHA:-unknown}"
    dockerfile = (PROJECT_ROOT / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    assert "ARG COMMIT_SHA=unknown" in dockerfile
    assert "ENV COMMIT_SHA=${COMMIT_SHA}" in dockerfile
