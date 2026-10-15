"""A stand-in for the `docker` CLI that infra/deploy/deploy.sh calls, run as a
script in place of the real one (tests/unit/test_deploy_script.py).

Every call is appended to STUB_LOG as "<BOT_IMAGE> | <arguments>", and a call
whose record contains STUB_FAIL_ON exits 1 before it has any effect.

Without STUB_STATE it only answers the calls a polling deploy reads from:
`ps -q bot` with STUB_BOT_ID and `inspect` of an image with sha256:previous.

With STUB_STATE, a JSON file, it keeps the containers and the nginx upstream
the deploy works on and records what happened in its "events":

- "containers": [{"id", "name", "service", "image", "status", "restarts",
  "health": [...]}]. "health" is the healthcheck's verdicts in order: each
  wait_until_healthy probe takes the next one and the last one stays.
- "nginx": {"id", "inc", "applied"} while nginx runs, else null; "inc" and
  "applied" are the container names of bot_upstream.inc and .applied.
  Optional "starting": how many more probes find no master process yet;
  "refuse_from": the number of the `nginx -t -q` before a reload from which on
  the running nginx refuses its configuration; "refuse_render": true makes it
  refuse what the deploy renders into it.
- "nginx_starting": the "starting" of an nginx `up` starts, 0 by default.
- "bot_health": {image: [...]} for the bot containers `up --scale` starts.
- "tasks": {image: "<status> <restarts>"} for the worker and the scheduler
  `up --wait` starts, "running 0" by default.
"""

import json
import os
from pathlib import Path
import re
import sys
from typing import Any

COMPOSE_PREFIX = ["compose", "--env-file", ".env", "-f", "infra/docker-compose.yml"]
PROJECT = "tg-bot-template"
PORT = "8080"


def _record(args: list[str]) -> str:
    return re.sub(r"\s+", " ", f"{os.environ.get('BOT_IMAGE', '')} | {' '.join(args)}")


def _legacy(args: list[str]) -> int:
    joined = " ".join(args)
    if joined.endswith("ps -q bot") and os.environ.get("STUB_BOT_ID"):
        print(os.environ["STUB_BOT_ID"])
    elif args[:3] == ["inspect", "-f", "{{.Image}}"]:
        print("sha256:previous")
    return 0


class Docker:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.containers: list[dict[str, Any]] = state.setdefault("containers", [])
        self.events: list[str] = state.setdefault("events", [])

    def _by_id(self, container_id: str) -> dict[str, Any] | None:
        for container in self.containers:
            if container["id"] == container_id:
                return container
        return None

    def _of(self, service: str, *, running_only: bool) -> list[dict[str, Any]]:
        return [
            container
            for container in self.containers
            if container["service"] == service
            and (container["status"] == "running" or not running_only)
        ]

    def _start(self, service: str, image: str, health: list[str]) -> None:
        taken = [
            int(container["name"].rsplit("-", 1)[1])
            for container in self.containers
            if container["service"] == service
        ]
        number = max(taken, default=0) + 1
        name = f"{PROJECT}-{service}-{number}"
        self.containers.append(
            {
                "id": f"{service}{number}",
                "name": name,
                "service": service,
                "image": image,
                "status": "running",
                "restarts": 0,
                "health": list(health),
            }
        )
        self.events.append(f"start {name} {image}")

    def compose(self, args: list[str]) -> int:
        if args[:2] == ["--profile", "webhook"]:
            args = args[2:]
        image = os.environ.get("BOT_IMAGE", "")
        if args[:2] == ["ps", "-q"] or args[:3] == ["ps", "-a", "-q"]:
            service = args[-1]
            if service == "nginx":
                if self.state.get("nginx"):
                    print(self.state["nginx"]["id"])
                return 0
            for container in self._of(service, running_only="-a" not in args):
                print(container["id"])
            return 0
        if args == ["rm", "-f", "bot"]:
            self.containers[:] = [
                c
                for c in self.containers
                if c["service"] != "bot" or c["status"] == "running"
            ]
            return 0
        if args[:2] == ["up", "-d"] and "--scale" in args:
            wanted = int(args[args.index("--scale") + 1].split("=")[1])
            health = self.state.get("bot_health", {}).get(image, ["healthy"])
            while len(self._of("bot", running_only=False)) < wanted:
                self._start("bot", image, health)
            return 0
        if args == ["up", "-d", "--no-deps", "nginx", "certbot"]:
            if not self.state.get("nginx"):
                self.state["nginx"] = {
                    "id": "nginx1",
                    "inc": ["bot"],
                    "applied": ["bot"],
                    "starting": self.state.get("nginx_starting", 0),
                }
                self.events.append("start nginx")
            return 0
        if args[:2] == ["up", "-d"] and args[-2:] == ["worker", "scheduler"]:
            status, restarts = (
                self.state.get("tasks", {}).get(image, "running 0").split()
            )
            for service in ("worker", "scheduler"):
                self.containers[:] = [
                    c for c in self.containers if c["service"] != service
                ]
                self._start(service, image, [])
                self.containers[-1]["status"] = status
                self.containers[-1]["restarts"] = int(restarts)
            return 0
        if args[:2] == ["exec", "-T"]:
            return self.nginx_exec(args[2:])
        return 0

    def _refuses(self, nginx: dict[str, Any]) -> bool:
        nginx["tests"] = nginx.get("tests", 0) + 1
        refuse_from = nginx.get("refuse_from")
        return refuse_from is not None and nginx["tests"] >= refuse_from

    def nginx_exec(self, args: list[str]) -> int:
        while args[0] == "-e":
            args = args[2:]
        assert args[0] == "nginx", args
        command = args[1:]
        nginx = self.state["nginx"]
        script = command[2] if command[:2] == ["sh", "-c"] else ""
        if "bot_upstream.inc.next" in script:
            nginx["inc"] = command[4:]
            self.events.append(f"write {' '.join(command[4:])}")
        elif "bot_upstream.applied.next" in script:
            nginx["applied"] = list(nginx["inc"])
        elif "worker process is shutting down" in script:
            return 1
        elif "nginx: master process" in script:
            if nginx.get("starting", 0) > 0:
                nginx["starting"] -= 1
                self.events.append("no master yet")
                return 1
        elif "/etc/nginx/bot/render.sh" in script:
            if nginx.get("refuse_render"):
                self.events.append("render refused")
                return 1
            self.events.append("render")
        elif command == ["nginx", "-t", "-q"]:
            if self._refuses(nginx):
                self.events.append("refused")
                return 1
            self.events.append("test")
        elif "bot_upstream.applied" in script:
            for name in nginx["applied"]:
                print(f"server {name}:{PORT} resolve;")
        elif command == ["nginx", "-s", "reload"]:
            self.events.append(f"reload {' '.join(nginx['inc'])}")
        else:
            raise AssertionError(f"unexpected exec in nginx: {command}")
        return 0

    def inspect(self, template: str, container_id: str) -> int:
        container = self._by_id(container_id)
        if container is None:
            return 1
        health = container["health"]
        if template == "{{.Image}}":
            print(container["image"])
        elif template == "{{.Name}}":
            print(f"/{container['name']}")
        elif template.startswith("{{.State.Status}} {{.RestartCount}} {{if"):
            verdict = health.pop(0) if len(health) > 1 else (health or [""])[0]
            print(f"{container['status']} {container['restarts']} {verdict}")
        elif template.startswith("{{.State.Status}} {{if"):
            print(f"{container['status']} {(health or [''])[0]}")
        elif template == "{{.State.Status}} {{.RestartCount}}":
            print(f"{container['status']} {container['restarts']}")
        else:
            raise AssertionError(f"unexpected inspect template: {template}")
        return 0

    def run(self, args: list[str]) -> int:
        if args[: len(COMPOSE_PREFIX)] == COMPOSE_PREFIX:
            return self.compose(args[len(COMPOSE_PREFIX) :])
        if args[:2] == ["inspect", "-f"]:
            return self.inspect(args[2], args[3])
        if args[0] == "stop":
            for container_id in args[1:]:
                container = self._by_id(container_id)
                if container is not None:
                    container["status"] = "exited"
                    self.events.append(f"stop {container['name']}")
            return 0
        if args[0] == "rm":
            for container_id in (arg for arg in args[1:] if arg != "-f"):
                container = self._by_id(container_id)
                if container is not None:
                    self.containers.remove(container)
                    self.events.append(f"remove {container['name']}")
            return 0
        return 0


def main(args: list[str]) -> int:
    record = _record(args)
    with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
        log.write(record + "\n")
    fail_on = os.environ.get("STUB_FAIL_ON")
    if fail_on and fail_on in record:
        return 1
    state_path = os.environ.get("STUB_STATE")
    if not state_path:
        return _legacy(args)
    path = Path(state_path)
    state = json.loads(path.read_text(encoding="utf-8"))
    status = Docker(state).run(args)
    path.write_text(json.dumps(state), encoding="utf-8")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
