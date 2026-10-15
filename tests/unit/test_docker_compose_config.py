import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import pytest
import yaml

from src.core.tasks.heartbeat import HEARTBEAT_INTERVAL_SECONDS
from src.main.config import BotConfig, WebhookConfig, WebServerConfig

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


# The prefix of each service's limit variables in .env (BOT_CPUS, BOT_MEMORY).
LIMIT_PREFIXES = {
    "postgres": "POSTGRES",
    "migrate": "MIGRATE",
    "bot": "BOT",
    "worker": "WORKER",
    "scheduler": "SCHEDULER",
    "redis": "REDIS",
    "nginx": "NGINX",
    "certbot": "CERTBOT",
}
WEBHOOK_SERVICES = frozenset({"nginx", "certbot"})
_LIMIT_DEFAULT = re.compile(r"\$\{(?P<name>[A-Z_]+):-(?P<default>[^}]+)\}")
_SIZE_UNITS = {"k": 2**10, "m": 2**20, "g": 2**30}


def _limit_default(value: object) -> tuple[str, str]:
    match = _LIMIT_DEFAULT.fullmatch(str(value))
    assert match, f"a limit must be ${{NAME:-default}}, got {value!r}"
    return match.group("name"), match.group("default")


def _size_in_bytes(size: str) -> int:
    """`512M` from compose, `512mb` from redis.conf."""
    match = re.fullmatch(r"(\d+)([kmg])b?", size.lower())
    assert match, size
    return int(match.group(1)) * _SIZE_UNITS[match.group(2)]


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


# Answers `config --images` with STUB_IMAGES and records every other call.
MAKE_DOCKER_STUB = """#!/bin/sh
case "$*" in
  *"config --images") printf '%s\\n' $STUB_IMAGES ;;
  *) printf '%s\\n' "$*" >>"$STUB_LOG" ;;
esac
"""
PINNED_IMAGE = "ghcr.io/owner/repo:sha-0123456789ab"


def _make(tmp_path: Path, target: str, bot_image: str) -> list[str]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "docker").write_text(MAKE_DOCKER_STUB, encoding="utf-8")
    (stubs / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()
    make = shutil.which("make")
    assert make is not None
    subprocess.run(
        [make, "--no-print-directory", target],
        cwd=PROJECT_ROOT,
        # Built from scratch: BOT_IMAGE exported in the developer's shell must
        # not decide which branch of the recipe runs.
        env={
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "STUB_IMAGES": f"{bot_image} telegram-bot-template-postgres:18 redis:8.10",
        },
        capture_output=True,
        check=True,
        timeout=60,
    )
    prefix = "compose --env-file .env -f infra/docker-compose.yml "
    return [call.removeprefix(prefix) for call in log.read_text().splitlines()]


@pytest.mark.parametrize(
    ("target", "local", "pinned"),
    [
        ("build", ["build"], ["build postgres"]),
        ("up", ["up -d"], ["up -d --no-build"]),
    ],
)
def test_make_never_builds_the_checkout_under_a_pinned_image(
    tmp_path: Path, target: str, local: list[str], pinned: list[str]
) -> None:
    """A build would put the checkout's code under the tag CI pushed, and the
    next pull of that tag would replace it without a word; `make run` and
    deploy.sh refuse it, so `make build` and `make up` must too."""
    local_dir = tmp_path / "local"
    pinned_dir = tmp_path / "pinned"
    local_dir.mkdir()
    pinned_dir.mkdir()
    assert _make(local_dir, target, "telegram-bot-template-bot:latest") == local
    assert _make(pinned_dir, target, PINNED_IMAGE) == pinned


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
    assert migrate["image"] == "telegram-bot-template-bot-dev:latest"
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


def test_the_dev_stack_never_takes_the_webhook_profile() -> None:
    """The dev stack forces polling; nginx started by COMPOSE_PROFILES=webhook
    in the shared .env would bind 80 and 443 and proxy to a bot with no webhook
    route."""
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\nDOCKER_COMPOSE_DEV = COMPOSE_PROFILES= docker compose " in makefile


def test_the_image_build_receives_the_commit() -> None:
    """/status and /health name the commit; the value exists only at build
    time, since .git never enters the image."""
    args = _services()["bot-builder"]["build"]["args"]
    assert args["COMMIT_SHA"] == "${COMMIT_SHA:-unknown}"
    dockerfile = (PROJECT_ROOT / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    assert "ARG COMMIT_SHA=unknown" in dockerfile
    assert "ENV COMMIT_SHA=${COMMIT_SHA}" in dockerfile


def test_every_service_takes_its_limits_from_the_environment() -> None:
    """A bot on a small VPS lowers its limits in .env; a limit written into the
    compose file would have to be edited on the server, and the next deploy's
    checkout would overwrite it."""
    services = _services()
    assert set(services) - {"bot-builder"} == set(LIMIT_PREFIXES)
    for service, prefix in LIMIT_PREFIXES.items():
        limits = services[service]["deploy"]["resources"]["limits"]
        cpus_name, cpus = _limit_default(limits["cpus"])
        memory_name, memory = _limit_default(limits["memory"])
        assert (cpus_name, memory_name) == (f"{prefix}_CPUS", f"{prefix}_MEMORY")
        assert re.fullmatch(r"\d+(\.\d+)?", cpus), (service, cpus)
        assert re.fullmatch(r"\d+[MG]", memory), (service, memory)


def test_env_example_documents_every_limit_with_its_default() -> None:
    """The commented line is how a developer learns the knob exists; a default
    there that differs from compose's would describe a limit nobody runs."""
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    services = _services()
    for service in LIMIT_PREFIXES:
        limits = services[service]["deploy"]["resources"]["limits"]
        for value in (limits["cpus"], limits["memory"]):
            name, default = _limit_default(value)
            assert f"\n# {name}={default}\n" in env_example, name


def test_redis_hits_maxmemory_before_its_container_limit() -> None:
    """Under noeviction a full Redis refuses writes, which every caller already
    survives; past the container limit the kernel kills it, and the task queue
    and FSM state with it."""
    redis_conf = (PROJECT_ROOT / "infra/redis.conf").read_text(encoding="utf-8")
    match = re.search(r"^maxmemory (\S+)$", redis_conf, re.MULTILINE)
    assert match
    limits = _services()["redis"]["deploy"]["resources"]["limits"]
    _, container_memory = _limit_default(limits["memory"])
    assert _size_in_bytes(container_memory) >= 1.5 * _size_in_bytes(match.group(1))


def test_only_nginx_faces_the_network() -> None:
    """The bot's health port and the webhook route stay on the compose network;
    Postgres and Redis are for an SSH tunnel to loopback. A new service that
    publishes a port has to be added here on purpose."""
    services = _services()
    assert services["nginx"]["ports"] == ["80:80", "443:443"]
    for name, service in services.items():
        if name == "nginx":
            continue
        for mapping in service.get("ports", []):
            assert mapping.startswith("127.0.0.1:"), (name, mapping)


def test_nginx_and_certbot_run_in_webhook_mode_only() -> None:
    """Polling needs no inbound port; a stray nginx would hold 80 and 443 on a
    box that has no certificate for them."""
    for name, service in _services().items():
        expected = ["webhook"] if name in WEBHOOK_SERVICES else None
        assert service.get("profiles") == expected, name


def test_nginx_gets_its_four_settings_and_no_secret() -> None:
    """The bot's env_file carries the token and every password; nginx renders
    its configuration from these four values only."""
    nginx = _services()["nginx"]
    assert "env_file" not in nginx
    assert set(nginx["environment"]) == {
        "WEBHOOK_BASE_URL",
        "WEBHOOK_PATH",
        "WEB_SERVER_PORT",
        "TELEGRAM_ALLOWED_SUBNETS",
    }
    web_server_port = WebServerConfig.model_fields["WEB_SERVER_PORT"].default
    webhook_path = WebhookConfig.model_fields["WEBHOOK_PATH"].default
    assert (
        nginx["environment"]["WEB_SERVER_PORT"]
        == f"${{WEB_SERVER_PORT:-{web_server_port}}}"
    )
    assert nginx["environment"]["WEBHOOK_PATH"] == f"${{WEBHOOK_PATH:-{webhook_path}}}"


def test_env_example_carries_the_subnets_nginx_defaults_to() -> None:
    """The commented line is the documentation of the default; one that differs
    from compose's would describe subnets nobody allows."""
    _, default = _limit_default(
        _services()["nginx"]["environment"]["TELEGRAM_ALLOWED_SUBNETS"]
    )
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert f"\n# TELEGRAM_ALLOWED_SUBNETS={default}\n" in env_example


def test_nginx_mounts_its_configuration_directory_whole() -> None:
    """A single-file bind mount stays on the old inode after `git pull`, so the
    re-render before a reload would read the previous files while `nginx -t`
    passed the new ones."""
    volumes = _services()["nginx"]["volumes"]
    assert "./nginx:/etc/nginx/bot:ro" in volumes
    assert not any(volume.startswith("./nginx/") for volume in volumes)


def test_certbot_and_nginx_share_the_certificate_and_the_challenge() -> None:
    """certbot writes, nginx only reads: a compromised nginx must not be able to
    replace the certificate it serves."""
    nginx = _services()["nginx"]["volumes"]
    certbot = _services()["certbot"]["volumes"]
    assert "certbot-certs:/etc/letsencrypt:ro" in nginx
    assert "certbot-webroot:/var/www/certbot:ro" in nginx
    assert "certbot-certs:/etc/letsencrypt" in certbot
    assert "certbot-webroot:/var/www/certbot" in certbot


def test_certbot_renews_twice_a_day_and_stops_on_sigterm() -> None:
    """Let's Encrypt recommends two renewal attempts a day; a loop that ignores
    SIGTERM would hold every `docker compose down` for the grace period."""
    script = " ".join(_services()["certbot"]["entrypoint"])
    assert "certbot renew" in script
    assert "sleep 12h & wait" in script
    assert "trap exit TERM" in script


def test_nginx_waits_for_a_healthy_bot() -> None:
    """The upstream is resolved at run time, so nginx starts without the bot;
    started early it would answer 502 to every webhook until the bot is up."""
    depends = _services()["nginx"]["depends_on"]
    assert depends["bot"] == {"condition": "service_healthy"}


def test_make_clean_keeps_the_certificate() -> None:
    """Let's Encrypt issues a certificate for one host at most five times a
    week; a few rounds of `make clean` while setting up a box would lock the
    webhook out for days."""
    compose = _load("infra/docker-compose.yml")
    certificate_volume = compose["volumes"]["certbot-certs"]["name"]
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    assert f"\nCERTIFICATE_VOLUME = {certificate_volume}\n" in makefile
    recipe = re.search(r"^clean:.*\n((?:\t.*\n)+)", makefile, re.MULTILINE)
    assert recipe
    commands = recipe.group(1).splitlines()
    assert not re.search(r"\bdown\b.*\s(-v|--volumes)\b", recipe.group(1))
    # certbot's image declares anonymous volumes, which `down` without -v keeps;
    # `rm --volumes` removes those of the stack's containers and nothing named.
    removal = next(i for i, line in enumerate(commands) if " rm --stop " in line)
    down = next(i for i, line in enumerate(commands) if " down " in line)
    assert removal < down
    assert commands[removal].strip() == (
        "$(DOCKER_COMPOSE) --profile webhook rm --stop --force --volumes"
    )
    assert f"label=com.docker.compose.project={compose['name']}" in recipe.group(1)
    assert "grep -vxF '$(CERTIFICATE_VOLUME)'" in recipe.group(1)
