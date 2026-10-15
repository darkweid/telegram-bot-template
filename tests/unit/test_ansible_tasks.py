from collections.abc import Iterator
from pathlib import Path
import re
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = PROJECT_ROOT / "infra" / "ansible"
COMMAND_MODULES = (
    "ansible.builtin.command",
    "ansible.builtin.shell",
    "command",
    "shell",
)
COMPOSE_CONFIG = re.compile(r"\bdocker\s+compose\b.*\bconfig\b", re.DOTALL)


def _tasks(node: Any) -> Iterator[dict[str, Any]]:
    """Every task in a tasks file or play, blocks included."""
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        if any(module in node for module in COMMAND_MODULES):
            yield node
        for key in ("tasks", "pre_tasks", "post_tasks", "block", "rescue", "always"):
            yield from _tasks(node.get(key))


def _command(task: dict[str, Any]) -> str:
    for module in COMMAND_MODULES:
        if module in task:
            args = task[module]
            return str(args.get("cmd", "") if isinstance(args, dict) else args)
    return ""


def _compose_config_tasks() -> list[tuple[str, dict[str, Any]]]:
    return [
        (f"{path.relative_to(ANSIBLE)}: {task.get('name')}", task)
        for path in sorted(ANSIBLE.rglob("*.yml"))
        for task in _tasks(yaml.safe_load(path.read_text(encoding="utf-8")))
        if COMPOSE_CONFIG.search(_command(task))
    ]


def test_a_resolved_compose_model_never_reaches_the_output() -> None:
    """`docker compose config` resolves .env into the model: BOT_TOKEN and the
    passwords. A registered result is printed by `make server-reboot` with -v
    and by every failed task, so only the service names may be read in the
    open."""
    tasks = _compose_config_tasks()

    assert tasks
    for name, task in tasks:
        command = _command(task)
        if re.search(r"\bconfig\s+--services\b", command):
            continue
        assert task.get("no_log") is True, name
        assert "--no-interpolate" in command, name
        assert "--no-env-resolution" in command, name
