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

# A broadcast in one of these is over; only its final report may be left to send.
FINISHED_STATUSES: tuple[BroadcastStatus, ...] = (
    BroadcastStatus.COMPLETED,
    BroadcastStatus.CANCELLED,
    BroadcastStatus.FAILED,
)
