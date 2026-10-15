from dataclasses import dataclass

from src.core.llm.repository import (
    LLMUsageByPurpose,
    LLMUsageSummary,
    LLMUsageTopUser,
)


@dataclass(frozen=True, slots=True)
class LLMUsageStats:
    """LLM spending for /stats: since midnight UTC, the last 7 and 30 days;
    the breakdowns cover the last 30 days."""

    today: LLMUsageSummary
    week: LLMUsageSummary
    month: LLMUsageSummary
    by_purpose: list[LLMUsageByPurpose]
    top_users: list[LLMUsageTopUser]
