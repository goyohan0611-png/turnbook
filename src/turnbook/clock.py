"""Timestamps as the stores accept them: ISO 8601 with a time, in one declared clock."""
from __future__ import annotations

from datetime import datetime, timezone


def timestamp(value, clock):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("TIMESTAMP_REQUIRED")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("INVALID_TIMESTAMP") from None
    aware = result.utcoffset() is not None
    if clock == "UTC" and aware:
        return result.astimezone(timezone.utc)
    if clock == "SOURCE_LOCAL" and not aware:
        return result
    raise ValueError("CLOCK_DOMAIN_MISMATCH")
