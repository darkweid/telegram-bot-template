from enum import StrEnum


class BroadcastStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


# A broadcast in one of these blocks a new one and may be resumed.
ACTIVE_STATUSES: tuple[BroadcastStatus, ...] = (
    BroadcastStatus.PENDING,
    BroadcastStatus.RUNNING,
)
