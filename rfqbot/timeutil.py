"""Time helpers shared by the recorders and their readers.

Every timestamp in the project is epoch milliseconds (UTC). Upstream strings arrive in several
spellings: Gamma's ``2026-10-25 13:30:00+00``, ISO ``2026-10-25T13:30:00Z`` / ``+00:00`` /
``-04:00``, and bare dates or naive datetimes (``2026-10-22``, ``2026-10-22T00:00:00``). A naive
value is taken as UTC by default; converting it through the machine's local zone (``astimezone``
on a naive datetime) silently shifts it when the host is not in UTC, which is the bug this module
replaces.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone


def now_ms() -> int:
    """Current wall-clock time in epoch milliseconds."""
    return int(time.time() * 1000)


def parse_iso_ms(s: str | None, *, assume_utc: bool = True) -> int | None:
    """Parse an ISO-8601 timestamp to epoch milliseconds; None when absent or unparseable.

    Python 3.11's ``fromisoformat`` accepts ``Z``, ``+HH:MM`` / ``-HH:MM``, Gamma's short ``+HH``,
    a space or ``T`` separator, fractional seconds and bare dates. A naive timestamp is UTC when
    ``assume_utc`` is true (the default; every feed we read reports UTC) and local time otherwise.
    """
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc) if assume_utc else dt.astimezone()
    return int(dt.timestamp() * 1000)
