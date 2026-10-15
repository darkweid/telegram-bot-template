from dataclasses import dataclass

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.llm.errors import LLMOutputError
from src.core.llm.interface import LLMClient
from src.core.llm.types import LLMMessage, LLMRequest
from src.note.exceptions import NoteSummaryFailedError
from src.note.prompts import (
    NOTE_SUMMARY_PURPOSE,
    NoteSummary,
    note_summary_system_prompt,
    note_summary_user_message,
)
from src.note.repositories import NoteRepository

# Bounds the input tokens of one call.
SUMMARY_NOTE_LIMIT = 20
# The longest valid answer (SUMMARY_MAX_CHARS of Russian text, the themes and
# the JSON around them) is about a thousand tokens. An answer cut at the limit
# is not repaired, so the limit keeps twice that; only tokens produced are paid.
SUMMARY_MAX_TOKENS = 2048


@dataclass(frozen=True, slots=True)
class NoteSummaryResult:
    note_count: int
    summary: NoteSummary


class SummarizeNotesUseCase:
    """A summary of the user's latest notes in `locale`, or None when there
    are no notes to summarize."""

    def __init__(
        self, uow: SQLAlchemyUnitOfWork, notes: NoteRepository, llm: LLMClient
    ) -> None:
        self._uow = uow
        self._notes = notes
        self._llm = llm

    async def execute(self, user_id: int, locale: str) -> NoteSummaryResult | None:
        async with self._uow as uow:
            notes = await self._notes.latest_for_user(
                uow.session, user_id, SUMMARY_NOTE_LIMIT
            )
            # Copied inside the block: the rollback on exit expires the rows.
            texts = [note.text for note in notes]
        # Outside the transaction: the call takes seconds and must not hold a
        # pooled connection.
        if not texts:
            return None
        request = LLMRequest(
            purpose=NOTE_SUMMARY_PURPOSE,
            system=note_summary_system_prompt(locale),
            messages=(
                LLMMessage(role="user", content=note_summary_user_message(texts)),
            ),
            max_tokens=SUMMARY_MAX_TOKENS,
        )
        try:
            response = await self._llm.structured(request, NoteSummary)
        except LLMOutputError as error:
            # A cut or invalid answer: to the user, the summary failed.
            raise NoteSummaryFailedError(error.detail) from error
        return NoteSummaryResult(note_count=len(texts), summary=response.output)
