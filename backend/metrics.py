"""In-process observability metrics (no external dependency).

The registry aggregates counters and latency histograms on the application process. ``snapshot()``
returns a JSON-serializable dict consumed by ``GET /api/admin/metrics``. For multi-worker
deployments, poll that endpoint from each worker and scrape externally — the registry is per
process by design, not a distributed store.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass


class _Histogram:
    """Fixed-window latency histogram; keeps the most recent samples and reports percentiles."""

    def __init__(self, max_samples: int = 2000) -> None:
        self._samples: deque[float] = deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._samples.append(value)

    def count(self) -> int:
        with self._lock:
            return len(self._samples)

    def percentiles(self, *ps: int) -> dict[str, float | None]:
        with self._lock:
            if not self._samples:
                return {f"p{p}": None for p in ps}
            ordered = sorted(self._samples)
            n = len(ordered)
            out: dict[str, float | None] = {}
            for p in ps:
                idx = min(n - 1, max(0, int(round(p / 100.0 * (n - 1)))))
                out[f"p{p}"] = ordered[idx]
            return out


@dataclass
class Metrics:
    """Mutable, thread-safe metric registry."""

    _lock: threading.Lock = None  # type: ignore[assignment]
    requests_total: int = 0
    requests_by_status: dict = None  # type: ignore[assignment]
    ingestion_jobs_failed_total: int = 0
    request_duration: _Histogram = None  # type: ignore[assignment]
    gauges: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self.requests_by_status = {}
        self.request_duration = _Histogram()
        self.gauges = {}

    def inc_request(self, method: str, path: str, status: int) -> None:
        with self._lock:
            self.requests_total += 1
            key = f"{method} {path} {status}"
            self.requests_by_status[key] = self.requests_by_status.get(key, 0) + 1

    def observe_request_duration(self, ms: float) -> None:
        self.request_duration.observe(ms)

    def inc_ingestion_jobs_failed(self, n: int = 1) -> None:
        with self._lock:
            self.ingestion_jobs_failed_total += n

    def set_gauge(self, name: str, value: float | int) -> None:
        with self._lock:
            self.gauges[name] = value

    def snapshot(self) -> dict:
        with self._lock:
            dur = self.request_duration.percentiles(50, 95, 99)
            dur_out = {k: (round(v, 2) if v is not None else None) for k, v in dur.items()}
            dur_out["samples"] = self.request_duration.count()
            return {
                "requests_total": self.requests_total,
                "requests_by_status": dict(self.requests_by_status),
                "request_duration_ms": dur_out,
                "ingestion_jobs_failed_total": self.ingestion_jobs_failed_total,
                "gauges": dict(self.gauges),
            }


_metrics = Metrics()


def get_metrics() -> Metrics:
    """Return the process-wide registry."""
    return _metrics


def reset_metrics() -> None:
    """Replace the registry (used by tests to start from a clean slate)."""
    global _metrics
    _metrics = Metrics()
