"""Per-user daily query rate limiting (Phase 5, A-4).

A process-internal, thread-safe counter keyed by subject. Counts reset at each UTC
day boundary. This matches the project's process-internal observability model (no
external store such as Redis); counters are lost on process restart, which is
acceptable for the single-instance IT assistant. A ``daily_quota`` of 0 disables
limiting entirely.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _next_utc_midnight(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


class QueryRateLimiter:
    """Track how many queries each subject has made in the current UTC day."""

    def __init__(self, daily_quota: int) -> None:
        self._daily_quota = max(0, int(daily_quota))
        self._lock = threading.Lock()
        self._day = _utc_now().strftime("%Y-%m-%d")
        self._counts: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self._daily_quota > 0

    def _ensure_day(self, now: datetime) -> None:
        today = now.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._counts.clear()

    def hit(self, subject_id: str) -> tuple[bool, int, int]:
        """Record one query for ``subject_id``.

        Returns ``(allowed, remaining, retry_after_seconds)``. When ``allowed`` is
        False, ``retry_after_seconds`` is the seconds until the next UTC day resets
        the quota. The returned ``remaining`` is 0 in that case.
        """
        if not self.enabled:
            return True, self._daily_quota, 0
        now = _utc_now()
        with self._lock:
            self._ensure_day(now)
            used = self._counts.get(subject_id, 0)
            reset_at = _next_utc_midnight(now)
            retry_after = max(0, int((reset_at - now).total_seconds()))
            if used >= self._daily_quota:
                return False, 0, retry_after
            self._counts[subject_id] = used + 1
            return True, self._daily_quota - (used + 1), retry_after

    def reset(self) -> None:
        """Clear all counters (used by tests and operational tooling)."""
        with self._lock:
            self._counts.clear()
            self._day = _utc_now().strftime("%Y-%m-%d")
