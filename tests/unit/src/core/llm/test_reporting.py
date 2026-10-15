import logging
from typing import Any

import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.transport import Transport

from src.core.llm.enums import LLMProviderName
from src.core.llm.reporting import report_llm_call
from src.core.llm.types import LLMMessage, LLMRequest
from tests.fakes.llm import llm_response
from tests.helpers.logs import captured_logs

SECRET_PROMPT = "my diary entry about the surgery"


class _DiscardingTransport(Transport):
    def capture_envelope(self, envelope: Envelope) -> None:
        pass


def test_each_call_leaves_exactly_one_breadcrumb_without_content() -> None:
    """The log line already reaches Sentry as a breadcrumb through the logging
    integration, even though our loggers do not propagate; a second, explicit
    one would only double every call in the event's trail."""
    breadcrumbs: list[dict[str, Any]] = []

    def collect(crumb: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
        breadcrumbs.append(crumb)
        return crumb

    # The production setup: breadcrumbs from INFO, no events below CRITICAL.
    client = sentry_sdk.Client(
        dsn="https://public@sentry.invalid/1",
        transport=_DiscardingTransport,
        default_integrations=False,
        integrations=[
            LoggingIntegration(level=logging.INFO, event_level=logging.CRITICAL)
        ],
        before_breadcrumb=collect,
    )
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        report_llm_call(
            provider=LLMProviderName.ANTHROPIC,
            model="claude-sonnet-5-5",
            request=LLMRequest(
                purpose="note_summary",
                messages=[LLMMessage(role="user", content=SECRET_PROMPT)],
            ),
            response=llm_response("the answer about the surgery"),
            error=None,
            latency_ms=12,
        )
    client.close()

    [crumb] = breadcrumbs
    assert crumb["category"] == "src.core.llm.reporting"
    assert "purpose=note_summary" in crumb["message"]
    assert "surgery" not in repr(crumb)


def test_a_cancelled_call_logs_its_outcome_without_tokens() -> None:
    """A cancelled call has no response to read tokens from: the line says
    what happened and claims no cost."""
    with captured_logs("src.core.llm.reporting") as records:
        report_llm_call(
            provider=LLMProviderName.ANTHROPIC,
            model="claude-sonnet-5-5",
            request=LLMRequest(
                purpose="note_summary",
                messages=[LLMMessage(role="user", content=SECRET_PROMPT)],
            ),
            response=None,
            error=None,
            latency_ms=1500,
            cancelled=True,
        )
    [record] = records
    assert record.levelno == logging.WARNING
    line = record.getMessage()
    assert "outcome=Cancelled input_tokens=0 output_tokens=0" in line
    assert "cost_usd=unknown latency_ms=1500" in line
    assert "surgery" not in line
