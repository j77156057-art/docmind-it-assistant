"""Ingestion-job retry backoff persistence tests.

The backoff lives on the row (``next_attempt_at``) so a broken provider is not hammered in a
tight loop and the claim query skips jobs whose backoff has not elapsed yet.
"""
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path

from backend import QueryDatabase
from backend.db_models import IngestionJobRecord
from sqlalchemy import update


class IngestionBackoffTests(unittest.TestCase):
    def setUp(self):
        self.db = QueryDatabase(f"sqlite:///{(Path(tempfile.mkdtemp()) / 'q.db').as_posix()}")
        self.db.initialize()

    def tearDown(self):
        self.db.dispose()

    def _make_job(self, **kw):
        return self.db.enqueue_ingestion_job(job_type="import", document_id=1, version_id=1, **kw)

    def test_retry_writes_future_next_attempt_at(self):
        job = self._make_job(max_attempts=3)
        failed = self.db.fail_ingestion_job(
            job["job_id"], "embedding_unavailable", retryable=True, backoff_max_seconds=1800,
        )
        self.assertEqual(failed["status"], "queued")
        self.assertIsNotNone(failed["next_attempt_at"])
        next_at = datetime.fromisoformat(failed["next_attempt_at"])
        delta = next_at - datetime.now(timezone.utc)
        self.assertGreater(delta.total_seconds(), 0)
        self.assertLessEqual(delta.total_seconds(), 1800)

    def test_terminal_failure_clears_next_attempt_at(self):
        job = self._make_job(max_attempts=1)
        failed = self.db.fail_ingestion_job(job["job_id"], "parse_failed", retryable=False)
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["next_attempt_at"])

    def test_claim_skips_future_dated_job_until_due(self):
        job = self._make_job(max_attempts=3)
        self.db.fail_ingestion_job(
            job["job_id"], "embedding_unavailable", retryable=True, backoff_max_seconds=1800,
        )
        # Not yet due: claim returns nothing.
        self.assertIsNone(self.db.claim_ingestion_job("worker-a"))
        # Backdate next_attempt_at to the past: claim now succeeds.
        with self.db.engine.begin() as conn:
            conn.execute(
                update(IngestionJobRecord).where(IngestionJobRecord.id == job["job_id"])
                .values(next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
        claimed = self.db.claim_ingestion_job("worker-a")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["job_id"], job["job_id"])

    def test_requereue_clears_next_attempt_at(self):
        job = self._make_job(max_attempts=3)
        self.db.fail_ingestion_job(
            job["job_id"], "embedding_unavailable", retryable=True, backoff_max_seconds=1800,
        )
        again = self.db.enqueue_ingestion_job(
            job_type="import", document_id=1, version_id=1, max_attempts=3,
        )
        self.assertEqual(again["job_id"], job["job_id"])
        self.assertIsNone(again["next_attempt_at"])


if __name__ == "__main__":
    unittest.main()
