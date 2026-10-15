from enum import StrEnum


class LLMProviderName(StrEnum):
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"


class ModelTier(StrEnum):
    """Which configured model answers: a domain names the kind of work, the
    configuration names the model, and a fallback keeps the tier."""

    FAST = "fast"
    STRONG = "strong"


class LLMCallStatus(StrEnum):
    """How one provider call ended, as `llm_usage.status` stores it."""

    SUCCESS = "success"
    ERROR = "error"
