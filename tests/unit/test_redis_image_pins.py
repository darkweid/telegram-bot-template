from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load(relative_path: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    )
    return loaded


def test_test_redis_runs_the_image_the_stack_runs() -> None:
    """The integration suite proves behaviour against one Redis version; the
    test container and the CI service must run the deployed image, or a Lua
    script can pass there and fail in production."""
    deployed = _load("infra/docker-compose.yml")["services"]["redis"]["image"]
    local_test = _load("infra/docker-compose.test.yml")["services"]["redis"]["image"]
    ci = _load(".github/workflows/_ci.yml")["jobs"]["integration-tests"]["services"][
        "redis"
    ]["image"]

    assert local_test == deployed
    assert ci == deployed
