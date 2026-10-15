from datetime import datetime, timedelta

# A run renews its lease at least every minute (CHECKPOINT_INTERVAL_SECONDS in
# run_broadcast.py); five missed renewals mean its worker is gone.
STALE_AFTER = timedelta(minutes=5)


def stale_before(now: datetime) -> datetime:
    """A lease last renewed before this moment belongs to a dead run."""
    return now - STALE_AFTER
