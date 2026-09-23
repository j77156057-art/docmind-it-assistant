"""Asynchronous evaluation: the ``evaluate`` job type.

The pre-publish gate runs the golden set *inside* the publish request. This module covers the
other way to run it — as a queued job — and the properties that make it safe to rely on:

1. the API can queue a per-version evaluation and answers 202 with the job;
2. the worker runs it, stores the evaluation run and writes the audit trail;
3. a below-threshold verdict is a **successful** job: the measurement happened, and the verdict
   belongs on the run (where the publish gate reads it), not on the job's status;
4. a job that cannot be run at all fails terminally **without** touching the version's lifecycle
   state — an evaluation never indexed anything, so it must not be able to mark a version failed.

Why point 4 needs a test at all: the shared failure path used to reset or fail *any* job's target
version, so a failed evaluation (or withdraw) could take an indexed version out of its own state
machine as a side effect. Nothing in production ever queued those types, which is why the seam went
unnoticed.
"""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from admin_app import create_admin_app
from backend import AppSettings, DocumentSourceStore, QueryDatabase
from backend.embeddings import EmbeddingClient
from ingestion import DocumentIngestionService
from worker.runner import IngestionWorker


CONTENT = (
    "# 打印机\n## 驱动安装\nzebra printer driver 需要重新安装驱动。\n"
    "## 纸张设置\n请选择 A4 纸张并重新校准。\n"
)


class EvaluationJobTests(unittest.TestCase):
    def make_settings(self, root: str, *, gate_mode: str = "warn",
                      min_recall: float = 0.8, min_citation: float = 0.5) -> AppSettings:
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
            governance_mode="direct",
            # Imports must go through the queue (202 + job) rather than indexing inside the request,
            # otherwise there is no job seam to test.
            ingestion_worker_enabled=True,
            ingestion_max_attempts=3,
            ingestion_backoff_max_seconds=0,
            evaluation_gate_mode=gate_mode,
            evaluation_min_recall=min_recall,
            evaluation_min_citation_accuracy=min_citation,
            evaluation_top_k=5,
        )

    @staticmethod
    def headers(subject: str, roles: str) -> dict[str, str]:
        return {"X-Auth-Subject": subject, "X-Auth-Roles": roles}

    @staticmethod
    def embedding_client() -> EmbeddingClient:
        return EmbeddingClient(
            mode="hash", provider="builtin", model="hash-1024",
            base_url="http://hash.local", api_key="test",
        )

    def worker(self, settings: AppSettings):
        """A worker with no injected evaluation service: the lazy path builds it from settings."""
        database = QueryDatabase(settings.database_url)
        ingestion = DocumentIngestionService(
            database, self.embedding_client(),
            max_bytes=settings.document_max_bytes,
            chunk_max_chars=settings.chunk_max_chars,
            chunk_overlap_chars=settings.chunk_overlap_chars,
            max_characters=settings.document_max_characters,
            max_pages=settings.document_max_pages,
            require_review=settings.governance_mode == "review",
        )
        worker = IngestionWorker(
            settings=settings, database=database, ingestion=ingestion,
            sources=DocumentSourceStore(settings.project_root / "data" / "sources"),
            worker_id="eval-worker",
        )
        return worker, database

    def import_and_index(self, client: TestClient, settings: AppSettings, headers: dict) -> dict:
        """Import one document through the API, then let a worker index it."""
        response = client.post(
            "/api/admin/documents/import",
            headers=headers,
            files={"file": ("printer.md", CONTENT.encode("utf-8"), "text/markdown")},
            data={"source_key": "manual/printer", "access_scope": "public",
                  "classification": "internal"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        worker, database = self.worker(settings)
        try:
            self.assertGreaterEqual(worker.drain(), 1)
        finally:
            worker.close()
            database.dispose()
        return response.json()

    def seed_case(self, client: TestClient, headers: dict, **overrides):
        payload = {
            "case_key": "ev-01",
            "question": "打印机驱动怎么装",
            "expected_document_key": "manual/printer",
            "expected_heading": "驱动安装",
            "tags": "eval/job",
            "active": True,
        }
        payload.update(overrides)
        response = client.put("/api/admin/evaluation/cases", headers=headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def queue(client: TestClient, headers: dict, version_id: int, **body):
        request = {"trigger": "pre_publish", "document_version_id": version_id, "queue": True}
        request.update(body)
        return client.post("/api/admin/evaluation/runs", headers=headers, json=request)

    # -- tests -----------------------------------------------------------------
    def test_api_queues_a_per_version_evaluation(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            runner = self.headers("runner-1", "evaluation_runner")
            editor = self.headers("editor-1", "knowledge_editor")
            database = QueryDatabase(settings.database_url)
            try:
                with TestClient(application) as client:
                    imported = self.import_and_index(client, settings, editor)
                    queued = self.queue(client, runner, imported["version_id"])
                self.assertEqual(queued.status_code, 202, queued.text)
                job = queued.json()["job"]
                self.assertEqual(job["job_type"], "evaluate")
                self.assertEqual(job["status"], "queued")
                self.assertEqual(job["version_id"], imported["version_id"])
                self.assertEqual(job["document_id"], imported["document_id"])
                # The trigger travels with the job so the stored run records why it was run.
                self.assertEqual(job["payload"]["trigger"], "pre_publish")
                actions = [event["action"] for event in database.audit_events(limit=50)]
                self.assertIn("evaluation_run_queued", actions)
            finally:
                database.dispose()

    def test_queue_requires_a_version_and_rejects_an_unknown_one(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            runner = self.headers("runner-1", "evaluation_runner")
            editor = self.headers("editor-1", "knowledge_editor")
            with TestClient(application) as client:
                imported = self.import_and_index(client, settings, editor)
                missing_version = client.post(
                    "/api/admin/evaluation/runs", headers=runner,
                    json={"trigger": "manual", "queue": True},
                )
                unknown_version = self.queue(client, runner, 10 ** 6)
            # The queue identifies a job by (job_type, version_id), so a corpus-wide run has no key
            # to be deduplicated on and cannot be queued.
            self.assertEqual(missing_version.status_code, 400, missing_version.text)
            self.assertEqual(unknown_version.status_code, 404, unknown_version.text)
            self.assertIsNotNone(imported["version_id"])

    def test_worker_runs_the_queued_evaluation_and_records_the_run(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            runner = self.headers("runner-1", "evaluation_runner")
            editor = self.headers("editor-1", "knowledge_editor")
            with TestClient(application) as client:
                imported = self.import_and_index(client, settings, editor)
                self.seed_case(client, runner)
                queued = self.queue(client, runner, imported["version_id"])
                self.assertEqual(queued.status_code, 202, queued.text)

            worker, database = self.worker(settings)
            try:
                self.assertEqual(worker.drain(limit=1), 1)
                version_id = imported["version_id"]
                runs = database.list_evaluation_runs(limit=5, document_version_id=version_id)
                self.assertEqual(len(runs), 1, runs)
                self.assertEqual(runs[0]["trigger"], "pre_publish")
                self.assertEqual(runs[0]["status"], "succeeded")
                self.assertEqual(runs[0]["total_cases"], 1)
                jobs = [job for job in database.list_ingestion_jobs(limit=10)
                        if job["job_type"] == "evaluate"]
                self.assertEqual([job["status"] for job in jobs], ["succeeded"])
                actions = [event["action"] for event in database.audit_events(limit=50)]
                self.assertIn("evaluation_run", actions)
            finally:
                worker.close()
                database.dispose()

    def test_below_threshold_verdict_is_still_a_successful_job(self):
        with tempfile.TemporaryDirectory() as root:
            # A heading the corpus does not contain, with a hard gate: the run cannot pass. That is
            # a measurement, not a worker fault, so the job must end `succeeded` either way.
            settings = self.make_settings(root, gate_mode="block")
            application = create_admin_app(settings)
            runner = self.headers("runner-1", "evaluation_runner")
            editor = self.headers("editor-1", "knowledge_editor")
            with TestClient(application) as client:
                imported = self.import_and_index(client, settings, editor)
                self.seed_case(client, runner, expected_heading="不存在的章节")
                self.assertEqual(self.queue(client, runner, imported["version_id"]).status_code, 202)

            worker, database = self.worker(settings)
            try:
                outcome = worker.run_once()
                self.assertIsNotNone(outcome)
                self.assertEqual(outcome["status"], "succeeded")
                self.assertEqual(outcome["gate_result"], "block")
                runs = database.list_evaluation_runs(
                    limit=5, document_version_id=imported["version_id"],
                )
                self.assertEqual(runs[0]["gate_result"], "block")
                # The verdict is recorded on the run; the worker did not retry a measurement that
                # cannot change.
                job = [item for item in database.list_ingestion_jobs(limit=10)
                       if item["job_type"] == "evaluate"][0]
                self.assertEqual(job["attempts"], 1)
            finally:
                worker.close()
                database.dispose()

    def test_unrunnable_evaluation_fails_terminally_without_touching_the_version(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            runner = self.headers("runner-1", "evaluation_runner")
            with TestClient(application) as client:
                imported = self.import_and_index(client, settings, editor)
                # Neither an expected-refusal nor bound to a document: the golden set is malformed,
                # so the run cannot be executed at all.
                self.seed_case(client, runner, expected_document_key="")

            worker, database = self.worker(settings)
            try:
                version_id = imported["version_id"]
                indexed_state = database.document_version_reference(version_id)["status"]

                # 1) A payload the column would reject. Failing here names the cause instead of
                #    surfacing an IntegrityError as a generic retryable worker_error.
                database.enqueue_ingestion_job(
                    job_type="evaluate", document_id=imported["document_id"],
                    version_id=version_id, payload={"trigger": "bogus"},
                    created_by_subject_id="editor-1", request_id="r-eval-payload",
                )
                outcome = worker.run_once()
                self.assertEqual(outcome["status"], "failed")
                self.assertEqual(outcome["error_code"], "job_payload_invalid")
                self.assertEqual(
                    database.document_version_reference(version_id)["status"], indexed_state,
                )

                # 2) A well-formed payload over a corpus the golden set cannot evaluate. Re-queueing
                #    reuses the same (type, version) row, which is what an operator retry does.
                database.enqueue_ingestion_job(
                    job_type="evaluate", document_id=imported["document_id"],
                    version_id=version_id, payload={"trigger": "manual"},
                    created_by_subject_id="editor-1", request_id="r-eval-case",
                )
                outcome = worker.run_once()
                self.assertEqual(outcome["status"], "failed")
                self.assertEqual(outcome["error_code"], "evaluation_case_invalid")
                self.assertFalse(outcome["retryable"])
                self.assertEqual(
                    database.document_version_reference(version_id)["status"], indexed_state,
                )

                # The audit trail has to name what failed: an evaluation is not an indexing failure
                # and must not point a reader at the version's index state.
                actions = [event["action"] for event in database.audit_events(limit=50)]
                self.assertIn("evaluate_job_failed", actions)
                self.assertNotIn("document_index_failed", actions)
            finally:
                worker.close()
                database.dispose()


if __name__ == "__main__":
    unittest.main()
