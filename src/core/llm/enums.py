from enum import StrEnum


class LLMProviderName(StrEnum):
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"


class LLMCallStatus(StrEnum):
    """How one provider call ended, as `llm_usage.status` stores it."""

    SUCCESS = "success"
    ERROR = "error"
