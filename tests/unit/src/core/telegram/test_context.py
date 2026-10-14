from aiogram.types import Update
import sentry_sdk

from src.core.telegram.context import sentry_scope_for, update_context_from
from src.core.update_context import UpdateContext
from tests.factories.updates import (
    build_callback_update,
    build_message_update,
    build_my_chat_member_update,
)
from tests.helpers.sentry import scope_contexts, scope_tags, scope_user


def test_message_update_names_its_sender_and_chat() -> None:
    update = build_message_update(
        "hi", user_id=7, chat_id=-100, chat_type="group", update_id=11
    )
    assert update_context_from(update) == UpdateContext(
        update_id=11, update_type="message", user_id=7, chat_id=-100
    )


def test_callback_and_membership_updates() -> None:
    callback = update_context_from(build_callback_update("x", user_id=7))
    assert callback.update_type == "callback_query"
    member = update_context_from(
        build_my_chat_member_update("kicked", user_id=7, update_id=9)
    )
    assert member == UpdateContext(
        update_id=9, update_type="my_chat_member", user_id=7, chat_id=7
    )


def test_an_update_of_a_type_we_do_not_know_is_still_described() -> None:
    context = update_context_from(Update(update_id=5))
    assert context == UpdateContext(update_id=5, update_type="unknown")


def test_sentry_scope_carries_the_update_and_is_dropped_afterwards() -> None:
    context = UpdateContext(update_id=5, update_type="message", user_id=7, chat_id=8)
    outer = sentry_sdk.get_current_scope()
    with sentry_scope_for(context):
        scope = sentry_sdk.get_current_scope()
        assert scope is not outer
        assert scope_tags(scope)["update_type"] == "message"
        assert scope_user(scope) == {"id": "7"}
        assert scope_contexts(scope)["update"] == {"update_id": 5, "chat_id": 8}
    assert "update_type" not in scope_tags(outer)
    assert sentry_sdk.get_current_scope() is outer


def test_an_update_without_a_user_leaves_the_scope_user_unset() -> None:
    context = UpdateContext(update_id=5, update_type="poll")
    with sentry_scope_for(context):
        scope = sentry_sdk.get_current_scope()
        assert scope_tags(scope)["update_type"] == "poll"
        assert not scope_user(scope)
        assert scope_contexts(scope)["update"] == {"update_id": 5, "chat_id": None}
