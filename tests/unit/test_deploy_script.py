"""infra/deploy/deploy.sh run against a stand-in `docker` that records every
call: the order of the steps and the refusals are checked without a Docker
daemon, and nothing here can touch the developer's containers or images."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

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
# tests/fakes/docker_cli.py says what it records and answers.
DOCKER_STUB = f"""#!/bin/sh
exec "{sys.executable}" -I -S "{PROJECT_ROOT / 'tests/fakes/docker_cli.py'}" "$@"
"""
UNAME_STUB = """#!/bin/sh
echo "$STUB_UNAME"
"""
# The deploy waits between polls and before it checks the worker; the stand-in
# containers change state only when docker is called, so waiting buys nothing.
SLEEP_STUB = "#!/bin/sh\n"
TASKS_UP = f"{COMPOSE} up -d --no-deps --wait --wait-timeout 180 worker scheduler"


@dataclass
class Deploy:
    returncode: int
    output: str
    calls: list[str]
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def events(self) -> list[str]:
        events: list[str] = self.state.get("events", [])
        return events

    def running(self, service: str) -> list[str]:
        return [
            container["name"]
            for container in self.state["containers"]
            if container["service"] == service and container["status"] == "running"
        ]


def _deploy(
    tmp_path: Path,
    env_file: dict[str, str],
    *,
    uname: str = "Linux",
    state: dict[str, Any] | None = None,
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
    for name, body in (
        ("docker", DOCKER_STUB),
        ("uname", UNAME_STUB),
        ("sleep", SLEEP_STUB),
    ):
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
    state_file = tmp_path / "docker.json"
    if state is not None:
        state_file.write_text(json.dumps(state), encoding="utf-8")
        environment["STUB_STATE"] = str(state_file)
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
        json.loads(state_file.read_text(encoding="utf-8")) if state is not None else {},
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
        TASKS_UP,
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
    assert deploy.returncode == 1
    assert deploy.calls == [
        f"{LOCAL_IMAGE} | {COMPOSE} run --rm --no-deps -e NGINX_REQUIRE_TLS=1 "
        "nginx nginx -t"
    ]


def test_an_invalid_env_stops_the_deploy_before_docker(tmp_path: Path) -> None:
    """A malformed .env caught after the build would leave a half-started stack;
    the checker runs before the first docker call."""
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
    assert "BUILD=1 builds the local image" in deploy.output
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
        STUB_FAIL_ON=f"{LOCAL_IMAGE} | {TASKS_UP}",
    )
    assert deploy.returncode == 1
    assert "going back to sha256:previous" in deploy.output
    rollback = deploy.calls[_index(deploy.calls, "sha256:previous") :]
    assert [call for call in rollback if " up " in call] == [
        f"sha256:previous | {TASKS_UP}",
        f"sha256:previous | {COMPOSE} up -d --no-deps --force-recreate --wait "
        "--wait-timeout 120 bot",
    ]


def test_a_failing_polling_bot_goes_back_to_the_previous_image(
    tmp_path: Path,
) -> None:
    """Updates must keep being taken: a bot that never turns healthy gives way
    to the image that was running, and the worker is left alone."""
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
    # .env and the local tag still name the failed image after the rollback.
    assert "a later 'make run' or 'docker compose up -d' would start it again" in (
        deploy.output
    )


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


def test_a_failed_migration_exits_1_with_a_deploy_line(tmp_path: Path) -> None:
    """Every failure must be visible to the CD job as exit 1 and say why, and no
    new code may start behind a migration that did not apply."""
    deploy = _deploy(tmp_path, POLLING_ENV, STUB_FAIL_ON="run --rm --no-deps migrate")
    assert deploy.returncode == 1
    assert "[deploy] the deploy failed (exit 1)" in deploy.output
    assert not any(" bot" in call and "force-recreate" in call for call in deploy.calls)


def test_a_failed_rollback_still_rolls_the_bot_back(tmp_path: Path) -> None:
    """When the worker will not go back either, the bot must still leave the new
    image, or two versions of the code keep running together."""
    deploy = _deploy(
        tmp_path,
        POLLING_ENV,
        STUB_BOT_ID="c1",
        STUB_FAIL_ON="worker scheduler",
    )
    assert deploy.returncode == 1
    assert "[deploy] rollback failed" in deploy.output
    assert deploy.calls[-1] == (
        f"sha256:previous | {COMPOSE} up -d --no-deps --force-recreate --wait "
        "--wait-timeout 120 bot"
    )


@pytest.mark.parametrize("variable", ["BUILD", "PRUNE"])
def test_build_and_prune_accept_only_0_or_1(tmp_path: Path, variable: str) -> None:
    """A typo such as PRUNE=yes would otherwise read as "off" or "on" by
    accident of the comparison."""
    deploy = _deploy(tmp_path, POLLING_ENV, **{variable: "yes"})
    assert deploy.returncode == 1
    assert f"{variable} must be 0 or 1" in deploy.output
    assert deploy.calls == []


@pytest.mark.parametrize(
    ("fail_on", "message"),
    [
        ("force-recreate", "the bot did not become healthy and there is no previous"),
        ("worker scheduler", "the worker or the scheduler did not become"),
    ],
)
def test_a_first_deploy_has_nothing_to_go_back_to(
    tmp_path: Path, fail_on: str, message: str
) -> None:
    """With no earlier container there is no image to restore; the deploy must
    say so instead of attempting a rollback to nothing."""
    deploy = _deploy(tmp_path, POLLING_ENV, STUB_FAIL_ON=fail_on)
    assert deploy.returncode == 1
    assert message in deploy.output
    assert "no previous image to go back to" in deploy.output
    assert not any(call.startswith("sha256:") for call in deploy.calls)


def _container(
    service: str, number: int, image: str = "sha256:previous", **fields: Any
) -> dict[str, Any]:
    return {
        "id": f"{service}{number}",
        "name": f"tg-bot-template-{service}-{number}",
        "service": service,
        "image": image,
        "status": "running",
        "restarts": 0,
        "health": ["healthy"],
        **fields,
    }


def _serving_webhook_stack(*bots: dict[str, Any], pinned: list[str]) -> dict[str, Any]:
    return {
        "containers": [*bots, _container("worker", 1), _container("scheduler", 1)],
        "nginx": {"id": "nginx1", "inc": pinned, "applied": pinned},
    }


def _position(events: list[str], event: str) -> int:
    assert event in events, f"{event!r} not in {events}"
    return events.index(event)


NEW_BOT = "tg-bot-template-bot-2"
OLD_BOT = "tg-bot-template-bot-1"


def test_a_running_nginx_renders_the_new_configuration_before_the_roll(
    tmp_path: Path,
) -> None:
    """A reload reads the files nginx rendered when it started, not the checkout:
    without a render in the running container every reload of the roll would
    keep serving the previous configuration while `nginx -t` passed the new
    one."""
    deploy = _deploy(
        tmp_path,
        WEBHOOK_ENV,
        state=_serving_webhook_stack(_container("bot", 1), pinned=["bot"]),
    )
    assert deploy.returncode == 0, deploy.output
    render = _position(deploy.events, "render")
    assert deploy.events[render : render + 3] == [
        "render",
        "test the running nginx",
        "reload bot",
    ]
    assert render < _position(deploy.events, f"start {NEW_BOT} {LOCAL_IMAGE}")
    assert f"{LOCAL_IMAGE} | {COMPOSE} exec -T -e NGINX_REQUIRE_TLS=1 nginx sh " in (
        "\n".join(deploy.calls)
    )


def test_a_failing_worker_rolls_the_webhook_bot_back(tmp_path: Path) -> None:
    """No two versions of the code may keep running together: the bot goes back
    to the previous image through the same roll, with no update refused."""
    state = _serving_webhook_stack(_container("bot", 1), pinned=["bot"])
    state["tasks"] = {LOCAL_IMAGE: "restarting 2"}
    deploy = _deploy(tmp_path, WEBHOOK_ENV, state=state)
    assert deploy.returncode == 1
    assert "worker is restarting 2 15s after it started" in deploy.output
    assert "rolled back; the deploy failed" in deploy.output
    rollback = deploy.events[
        _position(deploy.events, "start tg-bot-template-worker-1 sha256:previous") :
    ]
    assert rollback == [
        "start tg-bot-template-worker-1 sha256:previous",
        "start tg-bot-template-scheduler-1 sha256:previous",
        f"write {NEW_BOT}",
        f"reload {NEW_BOT}",
        "start tg-bot-template-bot-3 sha256:previous",
        "write tg-bot-template-bot-3",
        "reload tg-bot-template-bot-3",
        f"stop {NEW_BOT}",
        f"remove {NEW_BOT}",
        "write bot",
        "reload bot",
    ]
    assert deploy.running("bot") == ["tg-bot-template-bot-3"]
    assert {
        container["image"]
        for container in deploy.state["containers"]
        if container["status"] == "running"
    } == {"sha256:previous"}
