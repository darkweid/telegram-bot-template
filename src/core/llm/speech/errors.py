from src.core.llm.errors import LLMNotConfigured


class TranscriberNotConfigured(LLMNotConfigured):
    """TRANSCRIBE_MODEL is empty: voice input is off in this deployment."""
