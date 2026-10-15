from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"


def _load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_a_release_promotes_the_image_ci_built() -> None:
    """vX.Y.Z must be the artifact the checks ran on and CD deployed. A rebuild
    from the tag would run the same code on newer base layers, and a tag on a
    commit CI never built would publish code no check ran on."""
    steps = _load()["jobs"]["release"]["steps"]
    uses = [step.get("uses", "") for step in steps]
    scripts = "\n".join(step.get("run", "") for step in steps)

    assert not [
        action for action in uses if action.startswith("docker/build-push-action")
    ]
    assert 'docker buildx imagetools inspect "${IMAGE}:${SHA_TAG}"' in scripts
    assert (
        'docker buildx imagetools create -t "${IMAGE}:${VERSION}" "${IMAGE}:${SHA_TAG}"'
        in scripts
    )
    assert "--verify-tag" in scripts


def test_only_a_version_tag_starts_a_release() -> None:
    """A push to a branch must never publish a versioned image or a release."""
    triggers = _load()[True]

    assert set(triggers) == {"push"}
    assert triggers["push"] == {"tags": ["v*"]}


def test_a_release_tag_is_checked_before_anything_is_published() -> None:
    """The `v*` glob also matches `v1` or `vnext`. Checked after the promotion,
    such a tag would already have put an image under a name no one can tell
    from a release."""
    first = _load()["jobs"]["release"]["steps"][0]

    assert first["env"]["VERSION"] == "${{ github.ref_name }}"
    assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in first["run"]
    assert "${{" not in first["run"]


def test_a_release_runs_without_a_checkout() -> None:
    """Every step talks to the registry or the API; a checkout would only pull
    the history, and gh then needs the repository named explicitly."""
    steps = _load()["jobs"]["release"]["steps"]
    create = next(
        step for step in steps if step.get("name") == "Create the GitHub Release"
    )

    assert not [
        step for step in steps if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert create["env"]["GH_REPO"] == "${{ github.repository }}"
