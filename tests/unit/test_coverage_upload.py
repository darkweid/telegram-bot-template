from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CI = PROJECT_ROOT / ".github" / "workflows" / "_ci.yml"
GATE = "vars.COVERALLS_ENABLED == 'true'"
COVERAGE_ARTIFACT_PREFIX = "coverage-"


def _jobs() -> dict[str, Any]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _gated(job: dict[str, Any], step: dict[str, Any]) -> bool:
    return GATE in f"{job.get('if', '')} {step.get('if', '')}"


def test_nothing_reaches_coveralls_until_the_gate_is_on() -> None:
    """A fork, or this repository before it is public, has no Coveralls project;
    an upload there fails on every push and posts nothing useful."""
    found = 0
    for name, job in _jobs().items():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("coverallsapp/github-action@"):
                found += 1
                assert _gated(job, step), name
                assert step.get("continue-on-error") is True, name
    assert found == 2


def test_coverage_artifacts_and_lookup_wait_for_the_gate() -> None:
    """With Coveralls off, a report artifact would sit in storage for 30 days
    unread, and the lookup would walk 50 commits on every push for a report no
    job uploads."""
    jobs = _jobs()
    artifact_steps = [
        (name, job, step)
        for name, job in jobs.items()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("actions/upload-artifact@")
        and str(step.get("with", {}).get("name", "")).startswith(
            COVERAGE_ARTIFACT_PREFIX
        )
    ]
    lookup = next(
        step for step in jobs["changes"]["steps"] if step.get("id") == "coverage"
    )

    assert len(artifact_steps) == 2
    for name, job, step in artifact_steps:
        assert _gated(job, step), name
    assert GATE in lookup["if"]


def test_a_disabled_gate_never_reruns_tests_on_a_verified_tree() -> None:
    """unit-tests also runs on a push whose tree already passed, but only to
    produce a report Coveralls needs. With the gate off that run would be pure
    waste on every squash merge."""
    condition = _jobs()["unit-tests"]["if"]

    assert (
        f"{GATE} && github.event_name == 'push' && needs.changes.outputs.coverage_run == ''"
        in condition
    )
