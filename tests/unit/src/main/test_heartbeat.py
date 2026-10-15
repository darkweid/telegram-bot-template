import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.core.tasks.heartbeat import HeartbeatScheduleSource
from src.main import heartbeat as healthcheck
from tests.fakes.redis import InMemoryRedis


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> InMemoryRedis:
    fake = InMemoryRedis()
    monkeypatch.setattr(healthcheck.Redis, "from_url", lambda _url: fake)
    return fake


async def test_the_healthcheck_passes_once_the_loop_has_beaten(
    redis: InMemoryRedis,
) -> None:
    """Docker restarts the scheduler on a failed check; a fresh one must pass."""
    assert not await healthcheck.check("redis://unused")
    source = HeartbeatScheduleSource("redis://unused")
    await source.startup()
    await source.get_schedules()
    await source.shutdown()
    assert await healthcheck.check("redis://unused")


def test_the_healthcheck_never_loads_aiogram() -> None:
    """The check runs every 30 s under the scheduler's 0.5 CPU cap and must end
    inside compose's 5 s timeout; importing aiogram alone took seconds there."""
    root = Path(__file__).resolve().parents[4]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import src.main.heartbeat, sys; print('aiogram' in sys.modules)",
        ],
        cwd=root,
        # Built from scratch: a variable exported in the developer's shell must
        # not decide what the import loads.
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"
