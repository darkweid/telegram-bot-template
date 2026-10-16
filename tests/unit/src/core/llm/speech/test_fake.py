import pytest

from tests.fakes.speech import FakeTranscriber, transcript


async def test_the_fake_refuses_what_the_adapter_refuses() -> None:
    """A test double that accepts bad input hides caller bugs."""
    fake = FakeTranscriber()
    with pytest.raises(ValueError):
        await fake.transcribe(b"", "audio/ogg", language=None, purpose="voice")
    assert fake.calls == []


async def test_the_fake_returns_queued_results_in_order() -> None:
    """Tests script a conversation of several voice messages."""
    fake = FakeTranscriber()
    first, second = transcript("one"), transcript("two")
    fake.queue(first)
    fake.queue(second)
    fake.queue(RuntimeError("boom"))
    assert (
        await fake.transcribe(b"a", "audio/ogg", language="Russian", purpose="p")
        is first
    )
    assert (
        await fake.transcribe(b"b", "audio/ogg", language=None, purpose="p") is second
    )
    with pytest.raises(RuntimeError):
        await fake.transcribe(b"c", "audio/ogg", language=None, purpose="p")
    assert [call.audio for call in fake.calls] == [b"a", b"b", b"c"]
    assert fake.calls[0].language == "Russian"


async def test_an_empty_queue_fails_the_test() -> None:
    """The fake cannot invent a transcript."""
    with pytest.raises(AssertionError, match="no transcript queued"):
        await FakeTranscriber().transcribe(
            b"x", "audio/ogg", language=None, purpose="p"
        )
