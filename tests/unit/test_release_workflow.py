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
