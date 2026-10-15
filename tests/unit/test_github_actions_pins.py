from pathlib import Path
import re

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
RELEASE_COMMENT = re.compile(r"^\s+# v\d+\.\d+\.\d+$")


def test_every_third_party_action_is_pinned_to_a_commit() -> None:
    """A tag can be moved to other code by whoever controls the action's
    repository, and the next run executes it with this repository's token;
    a commit SHA cannot move. The comment keeps the release readable."""
    files = [
        *(PROJECT_ROOT / ".github").rglob("*.yml"),
        *(PROJECT_ROOT / ".github").rglob("*.yaml"),
    ]
    found = 0
    for path in files:
        for reference, rest in USES.findall(path.read_text(encoding="utf-8")):
            if reference.startswith("./"):
                continue
            found += 1
            assert PINNED.match(reference), f"{path.name}: {reference}"
            assert RELEASE_COMMENT.match(rest), f"{path.name}: {reference}{rest}"

    assert found


def test_every_job_runs_on_a_github_hosted_runner() -> None:
    """A self-hosted runner on a public repository runs fork pull requests on
    the runner owner's machine; GitHub-hosted minutes are free for public repos."""
    found = 0
    for path in (PROJECT_ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, job in workflow["jobs"].items():
            if "uses" in job:
                continue  # a call to a reusable workflow; its own jobs are checked
            found += 1
            assert job["runs-on"] == "ubuntu-latest", f"{path.name}: {name}"
    assert found


_RANK = {"none": 0, "read": 1, "write": 2}


def _grants(permissions: dict[str, str] | None) -> dict[str, str]:
    return permissions or {}


@pytest.mark.parametrize(
    ("reusable_name", "callers"),
    [
        ("_ci.yml", ("prod_ci.yml", "stage_ci.yml")),
        ("_deploy.yml", ("prod_deploy.yml", "stage_deploy.yml")),
    ],
)
def test_callers_grant_every_permission_the_reusable_workflow_requests(
    reusable_name: str, callers: tuple[str, ...]
) -> None:
    """A called workflow can never hold more than the job that calls it, so a
    permission the reusable workflow asks for and a caller omits fails the whole
    run at startup, before a single step."""
    workflows = PROJECT_ROOT / ".github/workflows"
    reusable = yaml.safe_load((workflows / reusable_name).read_text(encoding="utf-8"))
    requested = [
        (where, _grants(block.get("permissions")))
        for where, block in [
            ("workflow", reusable),
            *((f"job {name}", job) for name, job in reusable["jobs"].items()),
        ]
    ]

    found = 0
    for caller in callers:
        workflow = yaml.safe_load((workflows / caller).read_text(encoding="utf-8"))
        calling = [job for job in workflow["jobs"].values() if "uses" in job]
        assert calling, caller
        for job in calling:
            granted = _grants(job.get("permissions"))
            for where, wanted in requested:
                for scope, level in wanted.items():
                    found += 1
                    assert (
                        _RANK[granted.get(scope, "none")] >= _RANK[level]
                    ), f"{caller}: {where} requests {scope}: {level}"
    assert found
