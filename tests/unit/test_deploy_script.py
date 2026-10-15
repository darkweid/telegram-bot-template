"""infra/deploy/deploy.sh run against a stand-in `docker` that records every
call: the order of the steps and the refusals are checked without a Docker
daemon, and nothing here can touch the developer's containers or images."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "infra/deploy/deploy.sh"
LOCAL_IMAGE = "tg-bot-template-bot:latest"
PINNED_IMAGE = "ghcr.io/owner/repo:sha-0123456789ab"
COMPOSE = "compose --env-file .env -f infra/docker-compose.yml"
TOKEN = "123456789:" + "A" * 35
POLLING_ENV = {
    "BOT_TOKEN": TOKEN,
    "BOT_MODE": "polling",
    "COMPOSE_PROFILES": "",
    "POSTGRES_PASSWORD": "a-password",
    "REDIS_PASSWORD": "another-password",
}
WEBHOOK_ENV = {
    **POLLING_ENV,
    "BOT_MODE": "webhook",
    "COMPOSE_PROFILES": "webhook",
    "WEBHOOK_BASE_URL": "https://bot.example.com",
    "WEBHOOK_SECRET": "s" * 48,
}
# Records "<BOT_IMAGE> | <arguments>" per call. `ps -q bot` answers with
# STUB_BOT_ID (a running bot), `inspect` with an image id, and a call whose
# record contains STUB_FAIL_ON fails.
DOCKER_STUB = """#!/bin/sh
record="${BOT_IMAGE:-} | $*"
printf '%s\\n' "$record" >>"$STUB_LOG"
case "$*" in
  *"ps -q bot") [ -n "${STUB_BOT_ID:-}" ] && echo "$STUB_BOT_ID" ;;
  *"inspect -f {{.Image}} "*) echo sha256:previous ;;
esac
if [ -n "${STUB_FAIL_ON:-}" ]; then
  case "$record" in *"$STUB_FAIL_ON"*) exit 1 ;; esac
fi
exit 0
"""
UNAME_STUB = """#!/bin/sh
echo "$STUB_UNAME"
"""


@dataclass
class Deploy:
    returncode: int
    output: str
    calls: list[str]


def _deploy(
    tmp_path: Path,
    env_file: dict[str, str],
    *,
    uname: str = "Linux",
    **variables: str,
) -> Deploy:
    root = tmp_path / "checkout"
    (root / "infra/deploy").mkdir(parents=True)
    (root / "scripts/ops").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "infra/deploy/deploy.sh")
    shutil.copy(
        PROJECT_ROOT / "scripts/ops/check_env.py", root / "scripts/ops/check_env.py"
    )
    (root / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in env_file.items()),
        encoding="utf-8",
    )
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (("docker", DOCKER_STUB), ("uname", UNAME_STUB)):
        (stubs / name).write_text(body, encoding="utf-8")
        (stubs / name).chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()
    # Built from scratch: BUILD, BOT_IMAGE or PRUNE exported in the
    # developer's shell must not reach the script.
    environment = {
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_UNAME": uname,
        **variables,
    }
    completed = subprocess.run(
        ["bash", str(root / "infra/deploy/deploy.sh")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return Deploy(
        completed.returncode,
        completed.stdout + completed.stderr,
        log.read_text(encoding="utf-8").splitlines(),
    )


def _index(calls: list[str], fragment: str) -> int:
    matching = [number for number, call in enumerate(calls) if fragment in call]
    assert matching, f"no call contains {fragment!r}: {calls}"
    return matching[0]


def test_a_polling_deploy_runs_the_steps_in_order(tmp_path: Path) -> None:
    """Migrations before any new code, the bot before the worker and the
    scheduler: a failed migration leaves the previous stack running."""
    deploy = _deploy(tmp_path, POLLING_ENV)
    assert deploy.returncode == 0, deploy.output
    steps = [
        f"{COMPOSE} build --pull postgres",
        f"{COMPOSE} build bot-builder",
        f"{COMPOSE} up -d --wait postgres redis",
        f"{COMPOSE} run --rm --no-deps migrate",
        f"{COMPOSE} --profile webhook rm -sf nginx certbot",
        f"{COMPOSE} up -d --no-deps --force-recreate --wait --wait-timeout 120 bot",
        f"{COMPOSE} up -d --no-deps --wait worker scheduler",
    ]
    positions = [_index(deploy.calls, step) for step in steps]
    assert positions == sorted(positions)


def test_polling_deploy_removes_the_webhook_services(tmp_path: Path) -> None:
    """A bot moved from webhook to polling would otherwise keep nginx holding
    ports 80 and 443 with nothing behind the webhook path."""
    deploy = _deploy(tmp_path, POLLING_ENV)
    assert deploy.returncode == 0, deploy.output
    assert f"{LOCAL_IMAGE} | {COMPOSE} --profile webhook rm -sf nginx certbot" in (
        deploy.calls
    )
    assert not any("nginx nginx -t" in call for call in deploy.calls)


def test_a_polling_deploy_never_runs_two_bots(tmp_path: Path) -> None:
    """Two pollers on one token make Telegram answer both with a conflict, so
    the polling bot is replaced in place, never scaled up beside the old one."""
    deploy = _deploy(tmp_path, POLLING_ENV)
    assert deploy.returncode == 0, deploy.output
    assert not any("--scale" in call for call in deploy.calls)


def test_webhook_deploy_demands_a_certificate(tmp_path: Path) -> None:
    """Without one, nginx would serve port 80 only and Telegram, which calls
    HTTPS, would deliver nothing; the deploy stops before anything changes."""
    deploy = _deploy(tmp_path, WEBHOOK_ENV, STUB_FAIL_ON="NGINX_REQUIRE_TLS=1")
    assert deploy.returncode != 0
    assert deploy.calls == [
        f"{LOCAL_IMAGE} | {COMPOSE} run --rm --no-deps -e NGINX_REQUIRE_TLS=1 "
        "nginx nginx -t"
    ]


def test_an_invalid_env_stops_the_deploy_before_docker(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path, {**POLLING_ENV, "BOT_TOKEN": "nope"})
    assert deploy.returncode == 1
    assert "BOT_TOKEN is not a token" in deploy.output
    assert deploy.calls == []


def test_build_0_requires_an_image(tmp_path: Path) -> None:
    """The local tag exists in no registry; a pull of it would fail halfway."""
    deploy = _deploy(tmp_path, POLLING_ENV, BUILD="0")
    assert deploy.returncode == 1
    assert "BUILD=0 requires BOT_IMAGE" in deploy.output
    assert deploy.calls == []


def test_build_1_refuses_a_pinned_image(tmp_path: Path) -> None:
    """A build would put this checkout's code under the pinned tag."""
    deploy = _deploy(tmp_path, POLLING_ENV, BUILD="1", BOT_IMAGE=PINNED_IMAGE)
    assert deploy.returncode == 1
    assert deploy.calls == []


@pytest.mark.parametrize("source", ["shell", "env file"])
def test_a_pinned_image_is_pulled_not_built(tmp_path: Path, source: str) -> None:
    """CD passes the image in the shell; a box pinned in .env deploys the same
    way without BUILD=0 spelled out."""
    if source == "shell":
        deploy = _deploy(tmp_path, POLLING_ENV, BUILD="0", BOT_IMAGE=PINNED_IMAGE)
    else:
        deploy = _deploy(tmp_path, {**POLLING_ENV, "BOT_IMAGE": PINNED_IMAGE})
    assert deploy.returncode == 0, deploy.output
    assert f"{PINNED_IMAGE} | {COMPOSE} pull bot" in deploy.calls
    assert not any("build bot-builder" in call for call in deploy.calls)
    assert all(call.startswith(f"{PINNED_IMAGE} | ") for call in deploy.calls)


def test_a_failing_worker_takes_the_stack_back(tmp_path: Path) -> None:
    """No two versions of the code may keep running together: the bot goes
    back to the image it ran before the deploy, with the worker."""
    deploy = _deploy(
        tmp_path,
        POLLING_ENV,
        STUB_BOT_ID="c1",
        STUB_FAIL_ON=f"{LOCAL_IMAGE} | {COMPOSE} up -d --no-deps --wait worker",
    )
    assert deploy.returncode == 1
    assert "going back to sha256:previous" in deploy.output
    rollback = deploy.calls[_index(deploy.calls, "sha256:previous") :]
    assert rollback == [
        f"sha256:previous | {COMPOSE} up -d --no-deps --wait worker scheduler",
        f"sha256:previous | {COMPOSE} up -d --no-deps --force-recreate --wait "
        "--wait-timeout 120 bot",
    ]


def test_a_failing_polling_bot_goes_back_to_the_previous_image(
    tmp_path: Path,
) -> None:
    deploy = _deploy(
        tmp_path,
        POLLING_ENV,
        STUB_BOT_ID="c1",
        STUB_FAIL_ON=f"{LOCAL_IMAGE} | {COMPOSE} up -d --no-deps --force-recreate",
    )
    assert deploy.returncode == 1
    assert deploy.calls[-1] == (
        f"sha256:previous | {COMPOSE} up -d --no-deps --force-recreate --wait "
        "--wait-timeout 120 bot"
    )
    assert not any("worker" in call for call in deploy.calls)


@pytest.mark.parametrize(
    ("uname", "prune", "prunes"),
    [
        ("Linux", None, True),
        ("Linux", "0", False),
        ("Darwin", None, False),
        ("Darwin", "1", False),
    ],
)
def test_prune_runs_only_on_the_linux_box(
    tmp_path: Path, uname: str, prune: str | None, prunes: bool
) -> None:
    """`image prune -a` removes every image no container uses, on the whole
    machine; on a developer's Mac that is every other project's images."""
    variables = {} if prune is None else {"PRUNE": prune}
    deploy = _deploy(tmp_path, POLLING_ENV, uname=uname, **variables)
    assert deploy.returncode == 0, deploy.output
    pruned = [call for call in deploy.calls if " prune " in call]
    if prunes:
        assert [call.split(" | ")[1] for call in pruned] == [
            "image prune -af --filter until=168h",
            "builder prune -f --filter until=168h",
        ]
    else:
        assert pruned == []


def test_env_values_are_never_run_as_shell(tmp_path: Path) -> None:
    """.env is data: sourcing it would run whatever a value contains."""
    marker = tmp_path / "ran"
    deploy = _deploy(tmp_path, {**POLLING_ENV, "PROJECT_NAME": f"$(touch {marker})"})
    assert deploy.returncode == 0, deploy.output
    assert not marker.exists()


def test_the_local_image_name_agrees_everywhere() -> None:
    """deploy.sh, compose and `make run` must mean the same tag by "the image
    built here", or a deploy would build one tag and start another."""
    script = SCRIPT.read_text(encoding="utf-8")
    assert f'BOT_IMAGE="${{BOT_IMAGE:-{LOCAL_IMAGE}}}"' in script
    services = yaml.safe_load(
        (PROJECT_ROOT / "infra/docker-compose.yml").read_text(encoding="utf-8")
    )["services"]
    for name in ("bot-builder", "migrate", "bot", "worker", "scheduler"):
        assert services[name]["image"] == f"${{BOT_IMAGE:-{LOCAL_IMAGE}}}", name
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(rf"^LOCAL_BOT_IMAGE = {re.escape(LOCAL_IMAGE)}$", makefile, re.M)
