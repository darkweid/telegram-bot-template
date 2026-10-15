import pytest

from src.core.limiter.pacer import Pacer


class FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _pacer(rate: float, time: FakeTime) -> Pacer:
    return Pacer(rate, clock=time.clock, sleep=time.sleep)


async def test_calls_are_spaced_by_the_rate() -> None:
    """At the configured rate Telegram never sees a burst above its limit."""
    time = FakeTime()
    pacer = _pacer(4, time)
    for _ in range(3):
        await pacer.wait()
    assert time.slept == [0.25, 0.25]


async def test_time_already_spent_counts_toward_the_interval() -> None:
    """A slow send uses up its share of the interval; waiting the full
    interval on top would halve the real rate."""
    time = FakeTime()
    pacer = _pacer(4, time)
    await pacer.wait()
    time.now += 0.1
    await pacer.wait()
    assert time.slept == pytest.approx([0.15])


async def test_a_late_call_goes_at_once() -> None:
    time = FakeTime()
    pacer = _pacer(4, time)
    await pacer.wait()
    time.now += 1
    await pacer.wait()
    assert time.slept == []


def test_the_rate_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        Pacer(0)
