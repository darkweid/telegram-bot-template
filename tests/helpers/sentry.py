from typing import Any

from sentry_sdk import Scope


def scope_tags(scope: Scope) -> dict[str, Any]:
    """The tags set on `scope`; the SDK offers no public reader."""
    return dict(scope._tags)


def scope_user(scope: Scope) -> dict[str, Any] | None:
    """The user set on `scope`; the SDK offers no public reader."""
    return scope._user


def scope_contexts(scope: Scope) -> dict[str, Any]:
    """The contexts set on `scope`; the SDK offers no public reader."""
    return dict(scope._contexts)
