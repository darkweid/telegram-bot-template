from pathlib import Path
import re
from typing import Any

import yaml

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
    for name in ("bot", "postgres", "redis"):
        assert override[name]["restart"] == "no", name


def test_dev_override_keeps_the_production_image_out_of_the_dev_stack() -> None:
    """The dev bot migrates itself from the mounted tree; the one-shot service
    must neither pull BOT_IMAGE nor run the baked-in migrations."""
    migrate = _load("infra/docker-compose.override.yml")["services"]["migrate"]
    assert migrate["image"] == "tg-bot-template-bot-dev:latest"
    assert migrate["command"] == ["true"]
