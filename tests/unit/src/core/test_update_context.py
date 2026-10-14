import asyncio

import pytest

from src.core.update_context import (
    UpdateContext,
    bind_update_context,
    get_update_context,
)


def test_no_context_outside_an_update() -> None:
    assert get_update_context() is None


def test_bound_context_is_visible_and_reset_afterwards() -> None:
    context = UpdateContext(update_id=7, update_type="message", user_id=42, chat_id=42)
    with bind_update_context(context) as bound:
        assert bound is context
        assert get_update_context() is context
    assert get_update_context() is None


def test_context_is_reset_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError):
        with bind_update_context(UpdateContext(update_id=1, update_type="message")):
            raise RuntimeError("boom")
    assert get_update_context() is None


def test_nested_binding_restores_the_outer_context() -> None:
    outer = UpdateContext(update_id=1, update_type="message")
    inner = UpdateContext(update_id=2, update_type="callback_query")
    with bind_update_context(outer):
        with bind_update_context(inner):
            assert get_update_context() is inner
        assert get_update_context() is outer


async def test_concurrent_updates_do_not_see_each_other() -> None:
    seen: dict[int, int | None] = {}

    async def handle(update_id: int) -> None:
        with bind_update_context(
            UpdateContext(update_id=update_id, update_type="message")
        ):
            await asyncio.sleep(0)
            context = get_update_context()
            seen[update_id] = context.update_id if context else None

    await asyncio.gather(handle(1), handle(2), handle(3))
    assert seen == {1: 1, 2: 2, 3: 3}
