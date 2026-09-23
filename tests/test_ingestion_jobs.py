"""Ingestion queue and asynchronous worker tests.

The properties under test are the operational promises of batch 2:
* a queued job is claimed exactly once, and only by one worker;
* a crashed worker's job is reclaimed, and gives up once its attempts are spent;
* a corrupt document fails immediately, a transient provider error is retried;
* an upload returns 202 and the version stays out of retrieval until the worker indexes it.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import update

from admin_app import create_admin_app
from backend import (
    AppSettings, DocumentSourceStore, GovernanceError, QueryDatabase,
)
from backend.db_models import IngestionJobRecord
from backend.embeddings import EmbeddingClient
from ingestion import DocumentIngestionService
from worker.main import build_worker
from worker.runner import IngestionWorker


CONTENT = "## 打印机驱动\nzebra printer driver 需要重新安装。\n"


class IngestionJobTests(unittest.TestCase):
    def make_settings(self, root: str, *, governance_mode: str = "direct",
                      worker_enabled: bool = True, max_attempts: int = 3,
                      backoff_max_seconds: int = 1800) -> AppSettings:
        project = Path(root)
        knowledge = project / "knowledge.md"
        knowledge.write_text("# IT\n", encoding="utf-8")
        web = project / "web" / "index.html"
        web.parent.mkdir(parents=True, exist_ok=True)
        web.write_text("<!doctype html>", encoding="utf-8")
        return AppSettings(
            project_root=project,
            environment="test",
            database_url=f"sqlite:///{(project / 'queries.db').as_posix()}",
            knowledge_path=knowledge,
            web_index_path=web,
            artifact_output_path=project / "artifacts",
            auth_mode="trusted_headers",
            auth_subject_salt="unit-test-subject-salt",
            log_level="CRITICAL",
            governance_mode=governance_mode,
            ingestion_worker_enabled=worker_enabled,
            ingestion_max_attempts=max_attempts,
            ingestion_backoff_max_seconds=backoff_max_seconds,
            ingestion_job_timeout_seconds=600,
            ingestion_heartbeat_seconds=30,
        )

    @staticmethod
    def headers(subject: str, roles: str) -> dict[str, str]:
        return {"X-Auth-Subject": subject, "X-Auth-Roles": roles}

    @staticmethod
    def import_document(client: TestClient, headers: dict[str, str], *,
                        content: str = CONTENT, filename: str = "printer.md",
                        source_key: str = "manual/printer", access_scope: str = "public"):
        return client.post(
            "/api/admin/documents/import",
            headers=headers,
            files={"file": (filename, content.encode("utf-8"), "text/markdown")},
            data={
                "source_key": source_key,
                "access_scope": access_scope,
                "classification": "internal",
            },
        )

    def worker_with_client(self, settings: AppSettings, client: EmbeddingClient):
        """Build a worker with a specific embedding client (to control provider behaviour)."""
        database = QueryDatabase(settings.database_url)
        ingestion = DocumentIngestionService(
            database, client,
            max_bytes=settings.document_max_bytes,
            chunk_max_chars=settings.chunk_max_chars,
            chunk_overlap_chars=settings.chunk_overlap_chars,
            max_characters=settings.document_max_characters,
            max_pages=settings.document_max_pages,
            require_review=settings.governance_mode == "review",
        )
        sources = DocumentSourceStore(settings.project_root / "data" / "sources")
        worker = IngestionWorker(
            settings=settings, database=database, ingestion=ingestion, sources=sources,
            worker_id="test-worker",
        )
        return worker, database

    @staticmethod
    def backdate(database: QueryDatabase, job_id: int, **values) -> None:
        with database.engine.begin() as connection:
            connection.execute(
                update(IngestionJobRecord).where(IngestionJobRecord.id == job_id).values(**values)
            )

    def test_claim_retry_and_cancel_semantics(self):
        with tempfile.TemporaryDirectory() as root:
            database = QueryDatabase(f"sqlite:///{(Path(root) / 'q.db').as_posix()}")
            database.initialize()
            try:
                version = database.begin_document_import(
                    source_key="k/1", title="T", mime_type="text/markdown",
                    content_sha256="h1", access_scope="public", classification="internal",
                    submitted_by_subject_id="s1", request_id="r1",
                )
                job = database.enqueue_ingestion_job(
                    job_type="import", document_id=version["document_id"],
                    version_id=version["version_id"], created_by_subject_id="s1",
                    request_id="r1", max_attempts=2,
                )
                self.assertEqual((job["status"], job["attempts"], job["max_attempts"]),
                                 ("queued", 0, 2))

                # Re-submitting the same version re-queues the same row instead of stacking jobs.
                again = database.enqueue_ingestion_job(
                    job_type="import", document_id=version["document_id"],
                    version_id=version["version_id"], max_attempts=2,
                )
                self.assertEqual(again["job_id"], job["job_id"])
                self.assertEqual(len(database.list_ingestion_jobs()), 1)

                claimed = database.claim_ingestion_job("worker-a")
                self.assertEqual(claimed["job_id"], job["job_id"])
                self.assertEqual((claimed["status"], claimed["attempts"]), ("running", 1))
                self.assertIsNone(database.claim_ingestion_job("worker-b"))
                with self.assertRaises(GovernanceError) as context:
                    database.enqueue_ingestion_job(
                        job_type="import", document_id=version["document_id"],
                        version_id=version["version_id"],
                    )
                self.assertEqual(context.exception.code, "job_already_running")

                self.assertTrue(database.heartbeat_ingestion_job(job["job_id"], "worker-a"))
                self.assertFalse(database.heartbeat_ingestion_job(job["job_id"], "worker-b"))

                retryable = database.fail_ingestion_job(
                    job["job_id"], "embedding_timeout", retryable=True, backoff_max_seconds=0,
                )
                self.assertEqual((retryable["status"], retryable["attempts"]), ("queued", 1))
                self.assertEqual(retryable["last_error_code"], "embedding_timeout")

                database.reset_document_version_for_retry(version["version_id"])
                self.assertEqual(database.claim_ingestion_job("worker-a")["attempts"], 2)
                terminal = database.fail_ingestion_job(
                    job["job_id"], "embedding_timeout", retryable=True, backoff_max_seconds=0,
                )
                self.assertEqual((terminal["status"], terminal["attempts"]), ("failed", 2))
                non_retryable = database.fail_ingestion_job(
                    job["job_id"], "parse_failed", retryable=False,
                )
                self.assertEqual(non_retryable["status"], "failed")

                retried = database.retry_ingestion_job(job["job_id"], actor_subject_id="admin-1")
                self.assertEqual((retried["status"], retried["attempts"]), ("queued", 0))
                self.assertIsNone(retried["last_error_code"])

                cancelled = database.cancel_ingestion_job(job["job_id"], actor_subject_id="admin-1")
                self.assertEqual(cancelled["status"], "cancelled")
                with self.assertRaises(GovernanceError) as context:
                    database.cancel_ingestion_job(job["job_id"])
                self.assertEqual(context.exception.code, "job_not_cancellable")

                listed = database.list_ingestion_jobs(status="cancelled")
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["title"], "T")
                self.assertEqual(listed[0]["version"], 1)
                with self.assertRaises(ValueError):
                    database.list_ingestion_jobs(status="bogus")
            finally:
                database.dispose()

    def test_stale_running_job_is_reclaimed_then_gives_up(self):
        with tempfile.TemporaryDirectory() as root:
            database = QueryDatabase(f"sqlite:///{(Path(root) / 'q.db').as_posix()}")
            database.initialize()
            abandoned = datetime.now(timezone.utc) - timedelta(hours=2)
            try:
                version = database.begin_document_import(
                    source_key="k/2", title="T2", mime_type="text/markdown",
                    content_sha256="h2", access_scope="public",
                )
                job = database.enqueue_ingestion_job(
                    job_type="import", document_id=version["document_id"],
                    version_id=version["version_id"], max_attempts=2,
                )
                database.claim_ingestion_job("worker-a")
                self.backdate(database, job["job_id"], heartbeat_at=abandoned)

                self.assertEqual(database.reclaim_stale_ingestion_jobs(timeout_seconds=30), 1)
                reclaimed = database.list_ingestion_jobs()[0]
                self.assertEqual(reclaimed["status"], "queued")
                self.assertEqual(reclaimed["last_error_code"], "job_abandoned")

                # Second abandonment exhausts the two attempts and the job fails terminally.
                database.claim_ingestion_job("worker-a")
                self.backdate(database, job["job_id"], heartbeat_at=abandoned)
                self.assertEqual(database.reclaim_stale_ingestion_jobs(timeout_seconds=30), 1)
                self.assertEqual(database.list_ingestion_jobs()[0]["status"], "failed")

                # A queued job older than the timeout is reported as a stalled queue.
                database.enqueue_ingestion_job(
                    job_type="import", document_id=version["document_id"], version_id=None,
                )
                stats = database.ingestion_queue_stats(timeout_seconds=600)
                self.assertEqual(stats["stale_queued"], 0)
                self.backdate(
                    database, database.list_ingestion_jobs()[0]["job_id"], created_at=abandoned,
                )
                self.assertEqual(database.ingestion_queue_stats(timeout_seconds=600)["stale_queued"], 1)
            finally:
                database.dispose()

    def test_async_import_returns_202_and_the_worker_publishes(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root, governance_mode="direct")
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            administrator = self.headers("administrator", "admin")
            database = QueryDatabase(settings.database_url)
            try:
                with TestClient(application) as client:
                    imported = self.import_document(client, editor)
                    self.assertEqual(imported.status_code, 202)
                    self.assertTrue(imported.json()["queued"])
                    self.assertGreater(imported.json()["job_id"], 0)
                    self.assertEqual(imported.json()["status"], "queued")

                    # The version exists but has no chunks yet: nothing is retrievable.
                    self.assertEqual(
                        database.lexical_search("zebra", subject_id="v1", roles=("viewer",)), [],
                    )
                    pending_jobs = client.get("/api/admin/ingestion/jobs", headers=editor)
                    self.assertTrue(pending_jobs.json()["worker_enabled"])
                    self.assertEqual(pending_jobs.json()["queue"]["queued"], 1)

                    worker, worker_database = build_worker(settings)
                    self.assertTrue(worker.publish_on_success)
                    try:
                        self.assertEqual(worker.drain(), 1)
                    finally:
                        worker_database.dispose()

                    jobs = client.get("/api/admin/ingestion/jobs", headers=editor)
                    documents = client.get("/api/admin/documents", headers=editor)
                    # The audit log needs audit.read, which an editor deliberately does not have.
                    audit = client.get("/api/admin/audit-events", headers=administrator)
                    ready = client.get("/health/ready")

                self.assertEqual(jobs.json()["items"][0]["status"], "succeeded")
                self.assertIsNotNone(jobs.json()["items"][0]["finished_at"])
                self.assertEqual(jobs.json()["queue"]["queued"], 0)
                self.assertEqual(jobs.json()["queue"]["succeeded"], 1)
                self.assertEqual(documents.json()["items"][0]["status"], "indexed")
                self.assertTrue(
                    database.lexical_search("zebra", subject_id="v1", roles=("viewer",)),
                )
                actions = [item["action"] for item in audit.json()["items"]]
                self.assertIn("document_index_completed", actions)
                self.assertEqual(ready.status_code, 200)
                self.assertFalse(ready.json()["ingestion"]["stalled"])
                self.assertNotIn("ingestion_queue", ready.json()["checks"])
            finally:
                database.dispose()

    def test_async_import_in_review_mode_stops_at_staged(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root, governance_mode="review")
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            reviewer = self.headers("reviewer-1", "knowledge_reviewer")
            publisher = self.headers("publisher-1", "knowledge_publisher")
            database = QueryDatabase(settings.database_url)
            try:
                with TestClient(application) as client:
                    imported = self.import_document(client, editor)
                    self.assertEqual(imported.status_code, 202)
                    self.assertTrue(imported.json()["review_required"])

                    worker, worker_database = build_worker(settings)
                    self.assertFalse(worker.publish_on_success)
                    try:
                        self.assertEqual(worker.drain(), 1)
                    finally:
                        worker_database.dispose()

                    documents = client.get("/api/admin/documents", headers=editor)
                    self.assertEqual(documents.json()["items"][0]["status"], "staged")
                    self.assertEqual(
                        database.lexical_search("zebra", subject_id="v1", roles=("viewer",)), [],
                    )

                    # The reviewed flow still works on a worker-indexed version.
                    reviewed = client.post(
                        "/api/admin/documents/"
                        f"{imported.json()['document_id']}/versions/{imported.json()['version']}"
                        "/review",
                        headers=reviewer, json={"decision": "approve", "comment": "ok"},
                    )
                    published = client.post(
                        "/api/admin/documents/"
                        f"{imported.json()['document_id']}/versions/{imported.json()['version']}"
                        "/publish",
                        headers=publisher, json={"comment": "上线"},
                    )
                self.assertEqual(reviewed.status_code, 200)
                self.assertEqual(published.status_code, 200)
                self.assertEqual(published.json()["version"]["status"], "indexed")
                self.assertTrue(
                    database.lexical_search("zebra", subject_id="v1", roles=("viewer",)),
                )
            finally:
                database.dispose()

    def test_corrupt_document_fails_without_spending_retries(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root, max_attempts=3)
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            with TestClient(application) as client:
                imported = client.post(
                    "/api/admin/documents/import",
                    headers=editor,
                    files={"file": ("broken.docx", b"this is not a docx package", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
                    data={"source_key": "manual/broken", "access_scope": "public",
                          "classification": "internal"},
                )
                self.assertEqual(imported.status_code, 202)

                worker, worker_database = build_worker(settings)
                try:
                    self.assertEqual(worker.drain(), 1)
                finally:
                    worker_database.dispose()

                jobs = client.get("/api/admin/ingestion/jobs", headers=editor)
                documents = client.get("/api/admin/documents", headers=editor)

            job = jobs.json()["items"][0]
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["last_error_code"], "parse_failed")
            self.assertEqual(job["attempts"], 1)
            self.assertEqual(documents.json()["items"][0]["status"], "failed")
            self.assertEqual(documents.json()["items"][0]["error_code"], "parse_failed")

    def test_transient_embedding_failure_is_retried_then_fails(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root, max_attempts=2, backoff_max_seconds=0)
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            administrator = self.headers("administrator", "admin")

            def unavailable(_request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("provider unreachable")

            failing = EmbeddingClient(
                mode="provider", provider="qwen", model="text-embedding-v3",
                base_url="https://embedding.example.com/v1", api_key="test-key",
                transport=httpx.MockTransport(unavailable),
            )
            try:
                with TestClient(application) as client:
                    imported = self.import_document(client, editor)
                    self.assertEqual(imported.status_code, 202)

                    worker, worker_database = self.worker_with_client(settings, failing)
                    try:
                        processed = worker.drain()
                    finally:
                        worker_database.dispose()

                    jobs = client.get("/api/admin/ingestion/jobs", headers=editor)
                    documents = client.get("/api/admin/documents", headers=editor)
                    audit = client.get("/api/admin/audit-events", headers=administrator)

                # Two attempts are made, in-process backoff or not, then the job gives up.
                self.assertEqual(processed, 2)
                job = jobs.json()["items"][0]
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["last_error_code"], "embedding_unavailable")
                self.assertEqual(job["attempts"], 2)
                self.assertEqual(documents.json()["items"][0]["status"], "failed")
                self.assertIn("document_index_failed", [item["action"] for item in audit.json()["items"]])
            finally:
                database = QueryDatabase(settings.database_url)
                database.dispose()

    def test_mistargeted_jobs_fail_terminally(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            database = QueryDatabase(settings.database_url)
            database.initialize()
            worker, worker_database = build_worker(settings)
            try:
                # An evaluation without a version has no gate to attach its verdict to. The job
                # types a worker *does* implement are covered in tests/test_evaluation_job.py; the
                # remaining guard is for a type reaching a worker that was never taught it, which
                # the CHECK constraint keeps unreachable today.
                database.enqueue_ingestion_job(job_type="evaluate", document_id=None)
                outcome = worker.run_once()
                self.assertEqual(outcome["status"], "failed")
                self.assertEqual(outcome["error_code"], "job_target_missing")

                # A version registered without a stored original cannot be indexed.
                version = database.begin_document_import(
                    source_key="manual/missing", title="Missing", mime_type="text/markdown",
                    content_sha256="h-missing", access_scope="public",
                )
                database.enqueue_ingestion_job(
                    job_type="import", document_id=version["document_id"],
                    version_id=version["version_id"],
                )
                outcome = worker.run_once()
                self.assertEqual(outcome["error_code"], "source_missing")
                self.assertEqual(outcome["status"], "failed")
                stored = [
                    item for item in database.list_ingestion_jobs()
                    if item["job_id"] == outcome["job_id"]
                ][0]
                self.assertEqual(stored["attempts"], 1)
            finally:
                worker_database.dispose()
                database.dispose()

    def test_reindex_and_withdraw_jobs_succeed(self):
        """reindex re-embeds an indexed version in place; withdraw takes it offline with an audit trail."""
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            embeddings = EmbeddingClient(
                mode="hash", provider="builtin", model="hash-1024",
                base_url="http://hash.local", api_key="test",
            )
            with TestClient(application) as client:
                a = self.import_document(client, editor, source_key="manual/reindex-a")
                b = self.import_document(client, editor, source_key="manual/withdraw-b")
                self.assertEqual(a.status_code, 202)
                self.assertEqual(b.status_code, 202)

            worker, worker_database = self.worker_with_client(settings, embeddings)
            try:
                self.assertEqual(worker.drain(), 2)
                succeeded = worker_database.list_ingestion_jobs(status="succeeded")
                self.assertEqual(len(succeeded), 2)
                va = succeeded[0]["version_id"]
                da = succeeded[0]["document_id"]
                vb = succeeded[1]["version_id"]
                db2 = succeeded[1]["document_id"]

                # reindex re-embedds an already-indexed version and leaves it indexed.
                worker_database.enqueue_ingestion_job(
                    job_type="reindex", document_id=da, version_id=va,
                    created_by_subject_id="editor-1", request_id="r-reindex",
                )
                self.assertEqual(worker.drain(), 1)
                self.assertEqual(worker_database.document_version_reference(va)["status"], "indexed")

                # withdraw takes the version offline and writes an audit trail.
                worker_database.enqueue_ingestion_job(
                    job_type="withdraw", document_id=db2, version_id=vb,
                    created_by_subject_id="editor-1", request_id="r-withdraw",
                )
                self.assertEqual(worker.drain(), 1)
                self.assertEqual(worker_database.document_version_reference(vb)["status"], "withdrawn")

                actions = [e["action"] for e in worker_database.audit_events(limit=50)]
                self.assertIn("document_withdraw", actions)
                self.assertIn("document_index_completed", actions)
            finally:
                worker_database.dispose()

    def test_job_endpoints_enforce_capabilities(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            reviewer = self.headers("reviewer-1", "knowledge_reviewer")
            viewer = {"X-Auth-Subject": "viewer-1", "X-Auth-Roles": "viewer", "X-Auth-Groups": "it"}
            database = QueryDatabase(settings.database_url)
            try:
                with TestClient(application) as client:
                    imported = self.import_document(client, editor)
                    job_id = imported.json()["job_id"]
                    denied_list = client.get("/api/admin/ingestion/jobs", headers=viewer)
                    allowed_list = client.get("/api/admin/ingestion/jobs", headers=editor)
                    denied_retry = client.post(
                        f"/api/admin/ingestion/jobs/{job_id}/retry", headers=reviewer,
                    )
                    active_retry = client.post(
                        f"/api/admin/ingestion/jobs/{job_id}/retry", headers=editor,
                    )
                    cancelled = client.post(
                        f"/api/admin/ingestion/jobs/{job_id}/cancel", headers=editor,
                    )
                    busy_cancel = client.post(
                        f"/api/admin/ingestion/jobs/{job_id}/cancel", headers=editor,
                    )
                    missing = client.post(
                        "/api/admin/ingestion/jobs/9999/cancel", headers=editor,
                    )

                self.assertEqual(denied_list.status_code, 403)
                self.assertEqual(allowed_list.status_code, 200)
                self.assertEqual(denied_retry.status_code, 403)
                # State conflicts answer 409: authorised, but the job is not in a retryable state.
                self.assertEqual(active_retry.status_code, 409)
                self.assertEqual(cancelled.status_code, 200)
                self.assertEqual(cancelled.json()["job"]["status"], "cancelled")
                self.assertEqual(busy_cancel.status_code, 409)
                self.assertEqual(missing.status_code, 404)
            finally:
                database.dispose()

    def test_queue_diagnostics_report_a_stalled_queue_without_failing_readiness(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root, worker_enabled=True)
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            database = QueryDatabase(settings.database_url)
            try:
                with TestClient(application) as client:
                    imported = self.import_document(client, editor)
                    self.backdate(
                        database, imported.json()["job_id"],
                        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
                    )
                    ready = client.get("/health/ready")
                    jobs = client.get("/api/admin/ingestion/jobs", headers=editor)
                self.assertEqual(ready.status_code, 200)
                self.assertTrue(ready.json()["ingestion"]["worker_enabled"])
                self.assertTrue(ready.json()["ingestion"]["stalled"])
                self.assertEqual(ready.json()["ingestion"]["stale"], 1)
                self.assertEqual(jobs.json()["queue"]["stale_queued"], 1)
            finally:
                database.dispose()


if __name__ == "__main__":
    unittest.main()
