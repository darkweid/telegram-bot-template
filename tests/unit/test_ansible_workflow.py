from pathlib import Path
import re

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ansible.yml"
CI_SCRIPTS = PROJECT_ROOT / "infra" / "ansible" / "tests" / "ci"


def test_the_ansible_workflow_starts_when_the_make_targets_it_runs_change() -> None:
    """The jobs run `make ansible-deps` and `make ansible-lint`, and the generic CI
    never does, so a broken recipe in the Makefile would otherwise merge untested."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = workflow[True]
    commands = [
        step.get("run", "")
        for job in workflow["jobs"].values()
        for step in job["steps"]
    ]

    assert any(re.search(r"\bmake\b", command) for command in commands)
    for event in ("pull_request", "push"):
        assert "Makefile" in triggers[event]["paths"], event
        assert "infra/requirements/ansible.*" in triggers[event]["paths"], event


def test_ci_only_ever_previews_the_playbooks() -> None:
    """The CI inventory is the runner itself, with root. With --check a run changes
    nothing on it; without, it would close SSH, replace Docker and enable ufw
    under the job that runs it."""
    invocations = [
        line
        for script in CI_SCRIPTS.glob("*.sh")
        for line in script.read_text(encoding="utf-8").splitlines()
        if "ansible-playbook" in line and not line.lstrip().startswith("#")
    ]

    assert invocations
    for line in invocations:
        assert "--check" in line, line
