"""Small helpers for turning DB values into JSON-friendly values."""
from datetime import datetime, timezone
from decimal import Decimal


def iso(dt: datetime | None) -> str | None:
    """DB timestamps are 'timestamp without time zone' holding UTC (Supabase default)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def num(value, ndigits: int = 2):
    """Decimal/float -> rounded float; None stays None."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        value = float(value)
    return round(float(value), ndigits)


def as_int(value):
    return None if value is None else int(value)
