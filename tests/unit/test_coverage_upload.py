from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CI = PROJECT_ROOT / ".github" / "workflows" / "_ci.yml"
GATE = "vars.COVERALLS_ENABLED == 'true'"


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def test_nothing_reaches_coveralls_until_the_gate_is_on() -> None:
    """A fork, or this repository before it is public, has no Coveralls project;
    an upload there fails on every push and posts nothing useful."""
    found = 0
    for name, job in _jobs().items():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("coverallsapp/github-action@"):
                found += 1
                condition = f"{job.get('if', '')} {step.get('if', '')}"
                assert GATE in condition, name
                assert step.get("continue-on-error") is True, name
    assert found == 2


def test_a_disabled_gate_never_reruns_tests_on_a_verified_tree() -> None:
    """unit-tests also runs on a push whose tree already passed, but only to
    produce a report Coveralls needs. With the gate off that run would be pure
    waste on every squash merge."""
    condition = _jobs()["unit-tests"]["if"]

    assert "needs.changes.outputs.coverage_run == ''" in condition
    assert (
        f"{GATE} && github.event_name == 'push' && needs.changes.outputs.coverage_run == ''"
        in condition
    )
