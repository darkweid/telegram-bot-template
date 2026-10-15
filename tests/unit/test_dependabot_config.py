from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT_ROOT / ".github" / "dependabot.yml"


def _updates() -> list[dict[str, Any]]:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["updates"]


def test_dependabot_moves_only_python_packages_and_actions() -> None:
    """Container images move by hand: a bot raising Postgres a major version
    would leave the data directory unreadable, and the image pins have tests of
    their own."""
    ecosystems = {
        update["package-ecosystem"]: update["directory"] for update in _updates()
    }

    assert ecosystems == {"pip": "/infra/requirements", "github-actions": "/"}


def test_dependabot_commits_read_as_conventional_commits() -> None:
    """main takes squash merges whose subject is the PR title; a `Chore:` title
    breaks the Conventional Commits history every other commit follows.
    Dependabot appends the colon itself."""
    for update in _updates():
        ecosystem = update["package-ecosystem"]
        assert update["commit-message"]["prefix"] == "chore", ecosystem


def test_dependabot_opens_pull_requests_monthly() -> None:
    """Every merged bump costs a CI run and a review; a weekly cadence on a
    template with no deployment buys freshness nobody is waiting for."""
    for update in _updates():
        ecosystem = update["package-ecosystem"]
        assert update["schedule"]["interval"] == "monthly", ecosystem
