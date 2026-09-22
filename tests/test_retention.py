"""Document retention (Phase 4, A-4): 先软后硬 lifecycle.

The `documents.expired_at` column marks a document once it passes its retention window (soft), and
after a grace window the row and all its children are physically removed (hard). These tests cover
the repository methods directly and the admin endpoints (auth + self-auditing).
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from admin_app import create_admin_app
from backend import AppSettings
from backend.database import QueryDatabase
from backend.db_models import (
    DocumentAclRecord, DocumentChunkRecord, DocumentRecord, DocumentVersionRecord,
    DocumentVersionReviewRecord, EvaluationCaseRecord, EvaluationCaseResultRecord,
    EvaluationRunRecord, IngestionJobRecord,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_document(session, *, document_id: int, age_days: int, expired_age_days: int | None = None,
                  title: str = "doc") -> int:
    """Insert a document (and a minimal version) directly.

    `age_days` sets created_at in the past; `expired_age_days` (if given) sets expired_at in the
    past so the document is already soft-marked and, if large enough, past the grace window.
    Returns the document id.
    """
    created = _now() - timedelta(days=age_days)
    expired_at = None if expired_age_days is None else _now() - timedelta(days=expired_age_days)
    session.add(DocumentRecord(
        id=document_id, source_key=f"doc/{document_id}", title=title, mime_type="text/plain",
        access_scope="restricted", classification="internal",
        created_at=created, updated_at=created, expired_at=expired_at,
    ))
    session.add(DocumentVersionRecord(
        id=document_id * 10, document_id=document_id, version=1,
        content_sha256=f"sha{document_id}", status="indexed", created_at=created,
    ))
    return document_id


class TestRetentionRepository(unittest.TestCase):
    def _db(self, retention_days: int = 10, grace: int = 5) -> QueryDatabase:
        root = Path(tempfile.mkdtemp(prefix="docmind-retention-"))
        db = QueryDatabase(
            f"sqlite:///{(root / 'queries.db').as_posix()}",
            retention_days=retention_days, retention_grace_days=grace,
        )
        db.initialize()
        return db

    def _count(self, db: QueryDatabase, table) -> int:
        with db._sessions() as session:
            return int(session.scalar(select(func.count()).select_from(table)) or 0)

    def test_preview_counts_soft_and_hard_due(self):
        db = self._db()
        with db._sessions.begin() as session:
            # Past retention but not yet soft-marked -> soft_due.
            _seed_document(session, document_id=1, age_days=400)
            # Already soft-marked and past grace -> hard_due.
            _seed_document(session, document_id=2, age_days=400, expired_age_days=400)
            # Fresh -> untouched.
            _seed_document(session, document_id=3, age_days=1)
        preview = db.retention_preview()
        self.assertEqual(preview["total_documents"], 3)
        self.assertEqual(preview["soft_due"], 1)
        self.assertEqual(preview["hard_due"], 1)
        self.assertEqual(preview["retention_days"], 10)
        self.assertEqual(preview["retention_grace_days"], 5)

    def test_soft_stage_marks_expired_at_and_is_idempotent(self):
        db = self._db()
        with db._sessions.begin() as session:
            _seed_document(session, document_id=1, age_days=400)
        # First soft run marks it; second run is a no-op.
        first = db.retention_purge(stage="soft")
        self.assertEqual(first["soft_marked"], 1)
        second = db.retention_purge(stage="soft")
        self.assertEqual(second["soft_marked"], 0)
        preview = db.retention_preview()
        self.assertEqual(preview["soft_due"], 0)
        self.assertEqual(preview["hard_due"], 0)  # grace not elapsed yet

    def test_hard_stage_removes_document_and_all_children(self):
        db = self._db()
        with db._sessions.begin() as session:
            _seed_document(session, document_id=1, age_days=400, expired_age_days=400)
            version_id = 10
            session.add(DocumentChunkRecord(
                document_version_id=version_id, ordinal=0, heading="", content="c",
                search_text="c", embedding=[0.1, 0.2], created_at=_now(),
            ))
            session.add(DocumentAclRecord(
                document_id=1, principal_type="user", principal_id="u1",
                created_by_subject_id="admin", created_at=_now(),
            ))
            session.add(IngestionJobRecord(
                document_id=1, version_id=version_id, job_type="import", status="queued",
                request_id="r1",
            ))
            session.add(DocumentVersionReviewRecord(
                document_version_id=version_id, action="approve", from_status="staged",
                to_status="indexed", actor_subject_id="admin", request_id="r1",
            ))
            case = EvaluationCaseRecord(case_key="c1", question="q")
            session.add(case)
            session.flush()
            run = EvaluationRunRecord(
                trigger="manual", status="succeeded", gate_mode="warn",
                document_version_id=version_id,
            )
            session.add(run)
            session.flush()
            session.add(EvaluationCaseResultRecord(run_id=run.id, case_id=case.id, detail={}))
        # Sanity: children exist before purge.
        self.assertEqual(self._count(db, DocumentVersionRecord), 1)
        self.assertEqual(self._count(db, DocumentChunkRecord), 1)
        self.assertEqual(self._count(db, EvaluationRunRecord), 1)

        result = db.retention_purge(stage="hard")
        self.assertEqual(result["hard_deleted"], 1)

        # Document and every child row are gone.
        self.assertEqual(self._count(db, DocumentRecord), 0)
        self.assertEqual(self._count(db, DocumentVersionRecord), 0)
        self.assertEqual(self._count(db, DocumentChunkRecord), 0)
        self.assertEqual(self._count(db, DocumentAclRecord), 0)
        self.assertEqual(self._count(db, IngestionJobRecord), 0)
        self.assertEqual(self._count(db, DocumentVersionReviewRecord), 0)
        self.assertEqual(self._count(db, EvaluationRunRecord), 0)
        self.assertEqual(self._count(db, EvaluationCaseResultRecord), 0)
        # The golden case is shared test data and must survive.
        self.assertEqual(self._count(db, EvaluationCaseRecord), 1)

    def test_both_stage_soft_marks_then_hard_deletes_after_grace(self):
        db = self._db()
        with db._sessions.begin() as session:
            _seed_document(session, document_id=1, age_days=400)
        # Both: marks it soft now (grace not elapsed) -> hard_deleted 0.
        both = db.retention_purge(stage="both")
        self.assertEqual(both["soft_marked"], 1)
        self.assertEqual(both["hard_deleted"], 0)
        # Simulate the grace window elapsing, then both again removes it.
        with db._sessions.begin() as session:
            row = session.get(DocumentRecord, 1)
            row.expired_at = _now() - timedelta(days=400)
        again = db.retention_purge(stage="both")
        self.assertEqual(again["soft_marked"], 0)
        self.assertEqual(again["hard_deleted"], 1)
        self.assertEqual(db.retention_preview()["total_documents"], 0)

    def test_invalid_stage_is_rejected(self):
        db = self._db()
        with self.assertRaises(ValueError):
            db.retention_purge(stage="bogus")


class TestRetentionEndpoints(unittest.TestCase):
    @staticmethod
    def _headers(subject: str, roles: str) -> dict[str, str]:
        return {"X-Auth-Subject": subject, "X-Auth-Roles": roles}

    def _client(self) -> TestClient:
        root = Path(tempfile.mkdtemp(prefix="docmind-retention-ep-"))
        (root / "knowledge.md").write_text("# IT\n", encoding="utf-8")
        admin_index = root / "admin.html"
        admin_index.write_text("<!doctype html>", encoding="utf-8")
        settings = AppSettings(
            project_root=root,
            environment="test",
            database_url=f"sqlite:///{(root / 'queries.db').as_posix()}",
            knowledge_path=root / "knowledge.md",
            admin_index_path=admin_index,
            artifact_output_path=root / "artifacts",
            retention_days=365,
            retention_grace_days=30,
            auth_mode="trusted_headers",
            auth_subject_salt="unit-test-subject-salt",
            log_level="CRITICAL",
        )
        return TestClient(create_admin_app(settings))

    def _seed(self, database, *, document_id: int, age_days: int,
              expired_age_days: int | None = None) -> None:
        with database._sessions.begin() as session:
            _seed_document(
                session, document_id=document_id, age_days=age_days,
                expired_age_days=expired_age_days, title=f"old-{document_id}",
            )

    def test_preview_requires_audit_read_and_counts(self):
        with self._client() as client:
            database = client.app.state.database
            self._seed(database, document_id=1, age_days=400)

            forbidden = client.get(
                "/api/admin/retention/preview",
                headers=self._headers("carol", "viewer"),
            )
            self.assertEqual(forbidden.status_code, 403)

            ok = client.get(
                "/api/admin/retention/preview",
                headers=self._headers("auditor", "admin"),
            )
            self.assertEqual(ok.status_code, 200, ok.text)
            body = ok.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["soft_due"], 1)
            self.assertEqual(body["retention_days"], 365)

    def test_purge_requires_document_write_and_hard_deletes(self):
        with self._client() as client:
            database = client.app.state.database
            # Already soft-marked and past grace, ready for hard delete.
            self._seed(database, document_id=1, age_days=400, expired_age_days=400)

            forbidden = client.post(
                "/api/admin/retention/purge", json={"stage": "hard"},
                headers=self._headers("carol", "viewer"),
            )
            self.assertEqual(forbidden.status_code, 403)

            ok = client.post(
                "/api/admin/retention/purge", json={"stage": "hard"},
                headers=self._headers("admin", "admin"),
            )
            self.assertEqual(ok.status_code, 200, ok.text)
            body = ok.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["hard_deleted"], 1)
            self.assertEqual(body["soft_marked"], 0)

            # The document is gone.
            self.assertEqual(database.retention_preview()["total_documents"], 0)
            # The purge action was recorded for audit.
            with database._sessions() as session:
                from backend.db_models import AuditEventRecord
                recorded = session.scalar(
                    select(func.count()).select_from(AuditEventRecord).where(
                        AuditEventRecord.action == "retention.purge"
                    )
                )
            self.assertEqual(int(recorded or 0), 1)

    def test_soft_purge_then_hard_purge_via_endpoints(self):
        with self._client() as client:
            database = client.app.state.database
            self._seed(database, document_id=1, age_days=400)

            soft = client.post(
                "/api/admin/retention/purge", json={"stage": "soft"},
                headers=self._headers("admin", "admin"),
            )
            self.assertEqual(soft.status_code, 200, soft.text)
            self.assertEqual(soft.json()["soft_marked"], 1)
            # Still present, just marked.
            self.assertEqual(database.retention_preview()["total_documents"], 1)

            # Elapse the grace window and purge hard.
            with database._sessions.begin() as session:
                row = session.get(DocumentRecord, 1)
                row.expired_at = _now() - timedelta(days=400)
            hard = client.post(
                "/api/admin/retention/purge", json={"stage": "hard"},
                headers=self._headers("admin", "admin"),
            )
            self.assertEqual(hard.json()["hard_deleted"], 1)
            self.assertEqual(database.retention_preview()["total_documents"], 0)


if __name__ == "__main__":
    unittest.main()
