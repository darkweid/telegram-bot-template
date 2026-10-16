from collections.abc import Iterator
from contextlib import contextmanager
import logging
from typing import Any

import sentry_sdk
from sentry_sdk import Scope
from sentry_sdk.envelope import Envelope
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.transport import Transport


def scope_tags(scope: Scope) -> dict[str, Any]:
    """The tags set on `scope`; the SDK offers no public reader."""
    return dict(scope._tags)


def scope_user(scope: Scope) -> dict[str, Any] | None:
    """The user set on `scope`; the SDK offers no public reader."""
    return scope._user


def scope_contexts(scope: Scope) -> dict[str, Any]:
    """The contexts set on `scope`; the SDK offers no public reader."""
    return dict(scope._contexts)


class _DiscardingTransport(Transport):
    def capture_envelope(self, envelope: Envelope) -> None:
        pass


class _BreadcrumbCollector:
    def __init__(self) -> None:
        self.breadcrumbs: list[dict[str, Any]] = []

    def __call__(self, crumb: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
        self.breadcrumbs.append(crumb)
        return crumb


@contextmanager
def collected_breadcrumbs() -> Iterator[list[dict[str, Any]]]:
    """Every breadcrumb recorded inside the block, under the production logging
    setup: breadcrumbs from INFO, no events below CRITICAL. Nothing is sent."""
    collector = _BreadcrumbCollector()
    client = sentry_sdk.Client(
        dsn="https://public@sentry.invalid/1",
        transport=_DiscardingTransport,
        default_integrations=False,
        integrations=[
            LoggingIntegration(level=logging.INFO, event_level=logging.CRITICAL)
        ],
        before_breadcrumb=collector,
    )
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            yield collector.breadcrumbs
    finally:
        client.close()
