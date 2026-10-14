from dataclasses import dataclass
from uuid import UUID

from src.broadcast.enums import BroadcastStatus


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Where a run stands: the last user handled and the counts so far."""

    cursor_user_id: int
    sent: int
    failed: int
    unreachable: int

    @property
    def processed(self) -> int:
        return self.sent + self.failed + self.unreachable


@dataclass(frozen=True, slots=True)
class BroadcastProgress:
    id: UUID
    status: BroadcastStatus
    total: int
    sent: int = 0
    failed: int = 0
    unreachable: int = 0

    @property
    def processed(self) -> int:
        return self.sent + self.failed + self.unreachable
