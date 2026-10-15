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
PROJECT = "telegram-bot-template"
SCRIPT = PROJECT_ROOT / "infra/deploy/deploy.sh"
LOCAL_IMAGE = f"{PROJECT}-bot:latest"
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
    # The deploy built its image, so the local tag still names the failed one.
    assert "that still names the failed image" in deploy.output
    assert "before any plain 'make run'" in deploy.output


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
        "name": f"{PROJECT}-{service}-{number}",
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


NEW_BOT = f"{PROJECT}-bot-2"
OLD_BOT = f"{PROJECT}-bot-1"


def test_a_first_webhook_deploy_starts_nginx_and_routes_to_the_new_bot(
    tmp_path: Path,
) -> None:
    """With nothing running yet there is nothing to pin or stop: nginx starts
    with the configuration it renders on its way up, and goes to the service
    name once the bot is healthy."""
    deploy = _deploy(tmp_path, WEBHOOK_ENV, state={"containers": [], "nginx": None})
    assert deploy.returncode == 0, deploy.output
    assert deploy.events == [
        "start nginx",
        f"start {OLD_BOT} {LOCAL_IMAGE}",
        f"write {OLD_BOT}",
        "test",
        f"reload {OLD_BOT}",
        "write bot",
        "test",
        "reload bot",
        f"start {PROJECT}-worker-1 {LOCAL_IMAGE}",
        f"start {PROJECT}-scheduler-1 {LOCAL_IMAGE}",
    ]
    assert deploy.running("bot") == [OLD_BOT]


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
    assert deploy.events[render : render + 3] == ["render", "test", "reload bot"]
    assert render < _position(deploy.events, f"start {NEW_BOT} {LOCAL_IMAGE}")
    assert f"{LOCAL_IMAGE} | {COMPOSE} exec -T -e NGINX_REQUIRE_TLS=1 nginx sh " in (
        "\n".join(deploy.calls)
    )


def test_a_just_started_nginx_is_reloaded_once_its_master_is_up(
    tmp_path: Path,
) -> None:
    """`nginx -s reload` finds the master through its pid file, which a
    just-started nginx writes only on its way up: a reload sent earlier fails,
    and the roll would give up on a healthy bot."""
    deploy = _deploy(
        tmp_path,
        WEBHOOK_ENV,
        state={"containers": [], "nginx": None, "nginx_starting": 2},
    )
    assert deploy.returncode == 0, deploy.output
    assert deploy.events[: deploy.events.index(f"reload {OLD_BOT}") + 1] == [
        "start nginx",
        f"start {OLD_BOT} {LOCAL_IMAGE}",
        f"write {OLD_BOT}",
        "no master yet",
        "no master yet",
        "test",
        f"reload {OLD_BOT}",
    ]


def test_a_configuration_the_running_nginx_refuses_stops_the_deploy_first(
    tmp_path: Path,
) -> None:
    """A reload exits 0 even when the master refuses the new files and keeps the
    old ones: the deploy must see the refusal before it rolls a bot onto a
    configuration nginx is not serving."""
    state = _serving_webhook_stack(_container("bot", 1), pinned=["bot"])
    state["nginx"]["refuse_render"] = True
    deploy = _deploy(tmp_path, WEBHOOK_ENV, state=state)
    assert deploy.returncode == 1
    assert (
        "the running nginx refuses this checkout's configuration; it keeps the previous one"
        in (deploy.output)
    )
    assert "the bot was not rolled and the deploy failed" in deploy.output
    assert deploy.events == ["render refused"]
    assert deploy.running("bot") == [OLD_BOT]


def test_a_reload_nginx_refuses_mid_roll_keeps_the_old_bot_serving(
    tmp_path: Path,
) -> None:
    """nginx never reloaded onto the new container, so it still routes to the
    old one: the new one goes and the old one must keep running."""
    state = _serving_webhook_stack(_container("bot", 1), pinned=["bot"])
    # The first test follows the render, the second pins the old container.
    state["nginx"]["refuse_from"] = 3
    deploy = _deploy(tmp_path, WEBHOOK_ENV, state=state)
    assert deploy.returncode == 1
    assert "the running nginx refuses its configuration; not reloading" in deploy.output
    assert f"nginx could not be moved onto {NEW_BOT}" in deploy.output
    assert "rolling the bot failed; the previous one keeps serving" in deploy.output
    assert f"reload {NEW_BOT}" not in deploy.events
    assert f"remove {NEW_BOT}" in deploy.events
    assert deploy.running("bot") == [OLD_BOT]
    assert deploy.state["nginx"]["applied"] == [OLD_BOT]
    assert not any("worker scheduler" in call for call in deploy.calls)


def test_a_roll_that_cannot_unpin_nginx_still_succeeds(tmp_path: Path) -> None:
    """Once the old container is gone nginx is pinned to the new one, which
    serves: failing the roll there would roll back a bot that works and report
    that the previous one keeps serving when it no longer runs."""
    state = _serving_webhook_stack(_container("bot", 1), pinned=["bot"])
    state["nginx"]["refuse_from"] = 4
    deploy = _deploy(tmp_path, WEBHOOK_ENV, state=state)
    assert deploy.returncode == 0, deploy.output
    assert f"nginx stays pinned to {NEW_BOT}, which serves" in deploy.output
    assert deploy.running("bot") == [NEW_BOT]
    assert deploy.state["nginx"]["applied"] == [NEW_BOT]


def test_a_webhook_roll_moves_nginx_before_the_old_bot_stops(tmp_path: Path) -> None:
    """Every update reaches exactly one bot: nginx is pinned to the old
    container while the new one starts, moved onto the new one once it is
    healthy, and only then is the old one stopped."""
    deploy = _deploy(
        tmp_path,
        WEBHOOK_ENV,
        state=_serving_webhook_stack(_container("bot", 1), pinned=["bot"]),
    )
    assert deploy.returncode == 0, deploy.output
    events = deploy.events
    pinned_old = _position(events, f"reload {OLD_BOT}")
    started = _position(events, f"start {NEW_BOT} {LOCAL_IMAGE}")
    pinned_new = _position(events, f"reload {NEW_BOT}")
    stopped = _position(events, f"stop {OLD_BOT}")
    assert pinned_old < started < pinned_new < stopped
    assert events[stopped:][:3] == [f"stop {OLD_BOT}", f"remove {OLD_BOT}", "write bot"]
    assert deploy.running("bot") == [NEW_BOT]
    assert deploy.state["nginx"]["applied"] == ["bot"]


def test_a_new_bot_that_never_turns_healthy_is_removed(tmp_path: Path) -> None:
    """A new container that fails its healthcheck has served nothing; it goes,
    nginx goes back to the old one, and the worker is never touched."""
    state = _serving_webhook_stack(_container("bot", 1), pinned=["bot"])
    state["bot_health"] = {LOCAL_IMAGE: ["starting", "unhealthy"]}
    deploy = _deploy(tmp_path, WEBHOOK_ENV, state=state)
    assert deploy.returncode == 1
    assert "rolling the bot failed; the previous one keeps serving" in deploy.output
    assert "The checkout stays at the new commit" in deploy.output
    events = deploy.events
    assert _position(events, f"remove {NEW_BOT}") < len(events) - 1
    assert f"stop {OLD_BOT}" not in events
    assert deploy.running("bot") == [OLD_BOT]
    assert deploy.state["nginx"]["applied"] == ["bot"]
    assert not any("worker scheduler" in call for call in deploy.calls)


def test_containers_left_by_an_interrupted_roll_are_stopped_first(
    tmp_path: Path,
) -> None:
    """After an interrupted cutover nginx is pinned to the container the roll
    moved it onto; the one it left must not come back into the rotation, and
    is stopped only once nginx is pinned away from it."""
    state = _serving_webhook_stack(
        _container("bot", 1), _container("bot", 2, LOCAL_IMAGE), pinned=[NEW_BOT]
    )
    deploy = _deploy(tmp_path, WEBHOOK_ENV, state=state)
    assert deploy.returncode == 0, deploy.output
    assert f"stopping {OLD_BOT}, left behind by an interrupted deploy" in deploy.output
    events = deploy.events
    assert (
        _position(events, f"reload {NEW_BOT}")
        < _position(events, f"stop {OLD_BOT}")
        < _position(events, f"start {PROJECT}-bot-3 {LOCAL_IMAGE}")
    )
    assert deploy.running("bot") == [f"{PROJECT}-bot-3"]


def test_a_worker_with_no_container_rolls_the_stack_back(tmp_path: Path) -> None:
    """A service with nothing left to inspect would otherwise pass the check, and
    the new bot would enqueue tasks no worker ever runs."""
    state = _serving_webhook_stack(_container("bot", 1), pinned=["bot"])
    state["tasks"] = {LOCAL_IMAGE: "gone 0"}
    deploy = _deploy(tmp_path, WEBHOOK_ENV, state=state)
    assert deploy.returncode == 1
    assert "worker has no container 15s after it started" in deploy.output
    assert "rolled back; the deploy failed" in deploy.output
    assert deploy.running("worker") == [f"{PROJECT}-worker-1"]
    assert deploy.running("bot") == [f"{PROJECT}-bot-3"]


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
        _position(deploy.events, f"start {PROJECT}-worker-1 sha256:previous") :
    ]
    assert rollback == [
        f"start {PROJECT}-worker-1 sha256:previous",
        f"start {PROJECT}-scheduler-1 sha256:previous",
        f"write {NEW_BOT}",
        "test",
        f"reload {NEW_BOT}",
        f"start {PROJECT}-bot-3 sha256:previous",
        f"write {PROJECT}-bot-3",
        "test",
        f"reload {PROJECT}-bot-3",
        f"stop {NEW_BOT}",
        f"remove {NEW_BOT}",
        "write bot",
        "test",
        "reload bot",
    ]
    assert deploy.running("bot") == [f"{PROJECT}-bot-3"]
    assert {
        container["image"]
        for container in deploy.state["containers"]
        if container["status"] == "running"
    } == {"sha256:previous"}


STOP_TIMEOUT_FLAGS = ("-t", "--time", "--timeout")


def _passes_a_stop_timeout(call: str) -> bool:
    return any(
        word in STOP_TIMEOUT_FLAGS or word.startswith(("--time=", "--timeout="))
        for word in call.split()
    )


def test_bot_stops_use_the_containers_grace_period(tmp_path: Path) -> None:
    """BOT_STOP_GRACE_PERIOD reaches Docker only as the container's
    stop_grace_period: a timeout on the command line would cut the bot's
    drain short whatever .env says."""
    polling = _deploy(tmp_path / "polling", POLLING_ENV)
    webhook = _deploy(
        tmp_path / "webhook",
        WEBHOOK_ENV,
        state=_serving_webhook_stack(_container("bot", 1), pinned=["bot"]),
    )
    assert polling.returncode == 0, polling.output
    assert webhook.returncode == 0, webhook.output
    recreate = [call for call in polling.calls if "--force-recreate" in call]
    stops = [call for call in webhook.calls if " stop " in f" {call} "]
    assert recreate and stops
    for call in recreate + stops:
        assert not _passes_a_stop_timeout(call), call
