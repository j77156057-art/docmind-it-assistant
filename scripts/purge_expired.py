"""Purge documents past their retention window. Intended to run from cron (先软后硬).

Soft-marks documents that have reached their retention age and hard-deletes those past the grace
window (with all child rows). Safe to run repeatedly: the soft stage is idempotent and the hard
stage only removes rows already marked and past the grace window. Run:

    .venv\\Scripts\\python scripts/purge_expired.py

Reads IT_DATABASE_URL / IT_RETENTION_DAYS / IT_RETENTION_GRACE_DAYS from the environment or .env.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make the project importable when invoked directly (python scripts/purge_expired.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import AppSettings, QueryDatabase, configure_logging, log_event  # noqa: E402


def main() -> int:
    settings = AppSettings.from_environment()
    configure_logging(settings)
    database = QueryDatabase(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout,
        connect_timeout=settings.database_connect_timeout,
        query_field_key=settings.query_field_key.get_secret_value(),
        retention_days=settings.retention_days,
        retention_grace_days=settings.retention_grace_days,
    )
    database.initialize()
    result = database.retention_purge(stage="both")
    log_event(
        logging.getLogger("docmind.it.retention"), logging.INFO, "retention_purge_run",
        soft_marked=result["soft_marked"], hard_deleted=result["hard_deleted"],
    )
    print(
        f"retention purge: soft_marked={result['soft_marked']} "
        f"hard_deleted={result['hard_deleted']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
