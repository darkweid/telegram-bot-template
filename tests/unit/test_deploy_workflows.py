from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
# caller -> (CI workflow file, gate variable, environment, branch)
CALLERS = {
    "prod_deploy.yml": ("prod_ci.yml", "PROD_DEPLOY_ENABLED", "production", "main"),
    "stage_deploy.yml": ("stage_ci.yml", "STAGE_DEPLOY_ENABLED", "staging", "stage"),
}


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _deploy_job() -> dict:
    return _load("_deploy.yml")["jobs"]["deploy"]


def _check_job() -> dict:
    return _load("_deploy.yml")["jobs"]["check"]


def _step(name: str, job: dict | None = None) -> dict:
    job = _deploy_job() if job is None else job
    steps = [step for step in job["steps"] if step.get("name") == name]
    assert len(steps) == 1, name
    return steps[0]


def _ci_step(name: str) -> dict:
    steps = [
        step
        for step in _load("_ci.yml")["jobs"]["build-and-push"]["steps"]
        if step.get("name") == name
    ]
    assert len(steps) == 1, name
    return steps[0]


@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_each_deploy_follows_the_ci_of_its_branch(caller: str) -> None:
    """workflow_run matches the triggering workflow by its display name: a CI
    renamed without its CD stops every deploy, and nothing fails to say so."""
    ci_file, _, environment, branch = CALLERS[caller]
    workflow = _load(caller)
    ci = _load(ci_file)
    # PyYAML reads the bare key `on` as the boolean True.
    assert workflow[True]["workflow_run"]["workflows"] == [ci["name"]]
    assert branch in ci[True]["push"]["branches"]
    job = workflow["jobs"]["deploy"]
    assert job["uses"] == "./.github/workflows/_deploy.yml"
    assert job["with"]["environment"] == environment
    assert job["with"]["branch"] == branch


@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_deploy_is_off_until_its_variable_turns_it_on(caller: str) -> None:
    """The template and every fresh fork have no server. Without the gate each
    merge would start a deploy that fails on missing SSH secrets."""
    _, gate, _, _ = CALLERS[caller]
    job = _load(caller)["jobs"]["deploy"]

    assert job["if"] == f"${{{{ vars.{gate} == 'true' }}}}"


def test_deploy_runs_only_after_a_successful_push_to_its_branch() -> None:
    """workflow_run always runs in the default branch's context, so github.ref is
    main even for a stage push; only head_branch tells them apart. A pull
    request's CI or a failed run must never deploy."""
    condition = _check_job()["if"]

    assert "github.event.workflow_run.conclusion == 'success'" in condition
    assert "github.event.workflow_run.event == 'push'" in condition
    assert "github.event.workflow_run.head_branch == inputs.branch" in condition
    assert "github.ref" not in condition


def test_deploys_to_one_environment_never_overlap() -> None:
    """Two merges in a row must not run two migrations against one database at
    once, and a cancelled deploy can stop halfway through a migration."""
    concurrency = _deploy_job()["concurrency"]

    assert concurrency["group"] == "deploy-${{ inputs.environment }}"
    assert concurrency["cancel-in-progress"] is False


def test_only_a_run_with_an_image_enters_the_deploy_queue() -> None:
    """GitHub keeps one waiting job per concurrency group. A documentation push
    that entered it would replace a code deploy waiting there and then find no
    image, so nothing would be deployed at all."""
    jobs = _load("_deploy.yml")["jobs"]
    deploy = jobs["deploy"]
    queued = [name for name, job in jobs.items() if "concurrency" in job]

    assert queued == ["deploy"]
    assert deploy["needs"] == "check"
    assert deploy["if"] == "needs.check.outputs.image == 'true'"
    assert "concurrency" not in _load("_deploy.yml")
    built = _step("Check that CI built the image", _check_job())
    assert "if" not in built
    assert _check_job()["outputs"]["image"] == "${{ steps.built.outputs.image }}"


def test_a_manual_deploy_takes_only_a_commit_of_its_branch() -> None:
    """A commit only `stage` ever held must not reach production by hand, and a
    blank tag deploys the environment's branch, not whichever ref the dispatch
    happened to run from."""
    step = _step("Resolve the commit to deploy", _check_job())
    script = step["run"]

    assert step["env"]["BRANCH"] == "${{ inputs.branch }}"
    assert 'git merge-base --is-ancestor "$sha" "origin/${BRANCH}"' in script
    assert 'git rev-parse --verify "origin/${BRANCH}^{commit}"' in script
    assert "github.sha" not in script
    assert "${{ inputs.image_tag }}" not in script


def test_the_server_runs_the_image_ci_built() -> None:
    """The server never builds on the CD path: a broken build fails in CI, not
    halfway through a production deploy. deploy.sh takes the image from
    BOT_IMAGE, the variable compose reads."""
    script = _step("Deploy over SSH")["run"]

    assert 'BUILD=0 BOT_IMAGE="${IMAGE}:${TAG}" bash infra/deploy/deploy.sh' in script
    assert "APP_IMAGE" not in script


def test_the_alert_never_fails_the_deploy() -> None:
    """The run's colour is the deploy's outcome. An alert channel that is down or
    not configured (an organization secret a Free plan withholds from a private
    repository arrives empty) must not turn a good deploy red."""
    step = _step("Telegram notification")

    assert step["continue-on-error"] is True
    assert "always()" in step["if"]
    assert step["env"]["ALERT_BOT_TOKEN"] == "${{ secrets.ALERT_BOT_TOKEN }}"
    assert step["env"]["ALERT_CHAT_ID"] == "${{ secrets.ALERT_CHAT_ID }}"
    assert "secrets." not in step["run"]
    assert "::warning::" in step["run"]


def test_the_build_check_names_the_job_ci_runs() -> None:
    """The deploy finds CI's image by the build job's display name. A renamed
    job makes every CD run end green with nothing deployed."""
    build_name = _load("_ci.yml")["jobs"]["build-and-push"]["name"]
    script = _step("Check that CI built the image", _check_job())["run"]

    assert f'endswith("{build_name}")' in script


def test_the_deploy_computes_the_image_the_way_ci_pushes_it() -> None:
    """A different path or tag recipe on either side deploys an image that was
    never pushed, and the pull fails halfway through a deploy."""
    ci_script = _ci_step("Compute image and tag")["run"]
    cd_script = _step("Compute image and tag")["run"]

    for recipe in (
        "tr '[:upper:]' '[:lower:]'",
        'echo "ghcr.io/${{ github.repository }}"',
        "cut -c1-12",
        'echo "tag=sha-${short_sha}"',
    ):
        assert recipe in ci_script, recipe
        assert recipe in cd_script, recipe
