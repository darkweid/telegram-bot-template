from datetime import UTC, datetime


def get_utc_now() -> datetime:
    """The application clock: offset-aware UTC. created_at/updated_at come from
    the database's func.now(), so patching this does not move them."""
    return datetime.now(UTC)
