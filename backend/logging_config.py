"""Structured, privacy-minimizing application logging."""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone

from .config import AppSettings


request_id_context: ContextVar[str] = ContextVar("it_request_id", default="-")
# Log fields are an allowlist, not a passthrough: a caller must not be able to leak content into
# the logs just by passing an extra keyword. Every entry below is an identifier, an enum, a
# counter or a boolean — never free-form text. Anything else is dropped silently, so operational
# code that needs a field must add it here deliberately (and content-carrying keys such as a
# question, an answer or a document body must never be added).
_EXTRA_FIELDS = (
    # HTTP request boundary
    "event", "method", "path", "status_code", "duration_ms", "component",
    "reason", "environment", "provider", "model", "error_type",
    # Indexing worker and job queue
    "worker_id", "engine", "publish_on_success", "poll_seconds", "max_attempts",
    "job_id", "job_type", "status", "error_code", "attempts", "terminal", "retryable",
    "count", "chunk_count", "version_id",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "request_id": request_id_context.get(),
        }
        for key in _EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value is not None and key != "event":
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(settings: AppSettings) -> None:
    handler = logging.StreamHandler()
    if settings.log_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S",
        ))
    logging.basicConfig(level=getattr(logging, settings.log_level), handlers=[handler], force=True)
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("httpx", "httpcore", "sqlalchemy", "alembic"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    logger.log(level, event, extra={"event": event, **fields})
