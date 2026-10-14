# ruff: noqa: E402 -- TESTING must be set before any src.* import triggers config load
import os

os.environ.setdefault("TESTING", "true")

from pathlib import Path

import pytest

INTEGRATION_ROOT = Path(__file__).resolve().parent
# tests/integration -> tests -> repo root.
REPO_ROOT = INTEGRATION_ROOT.parents[1]


def pytest_itemcollected(item: pytest.Item) -> None:
    """Mark everything collected under tests/integration as `integration`.

    A module-level `pytestmark` would do the same, but forgetting it in one new file is
    enough to leak a database-dependent test into `make test`, which runs without Docker.
    Marking by location makes that impossible.

    `pytest_itemcollected` fires as each item is created, so the marker is in place before
    anything reads it — no assumption about how this hook orders against the `-m`
    deselection pass, which `pytest_collection_modifyitems` would need.
    """
    if item.path is not None and INTEGRATION_ROOT in item.path.resolve().parents:
        item.add_marker(pytest.mark.integration)
