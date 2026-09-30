"""Small time helpers shared by the surface-movement reads and their arithmetic."""
from __future__ import annotations

from datetime import datetime, timezone


def as_utc(value: datetime | None) -> datetime:
    """A timezone-aware UTC datetime: a naive one is taken as UTC, and None means now."""
    if value is None:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
