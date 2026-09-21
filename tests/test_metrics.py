"""A-5 observability: GET /api/admin/metrics exposes a process-local snapshot.

The endpoint is gated by ``audit.read`` (auditor/admin). It aggregates request counters/latency
from the in-process registry plus queue depth and model spend pulled from the database.
"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from admin_app import create_admin_app
from backend import AppSettings
from backend.metrics import get_metrics, reset_metrics


class MetricsEndpointTests(unittest.TestCase):
    @staticmethod
    def headers(subject: str, roles: str) -> dict[str, str]:
        return {"X-Auth-Subject": subject, "X-Auth-Roles": roles}

    def _make_client(self) -> TestClient:
        root = tempfile.mkdtemp(prefix="docmind-metrics-")
        project = Path(root)
        (project / "knowledge.md").write_text("# IT\n", encoding="utf-8")
        admin_index = project / "admin.html"
        admin_index.write_text("<!doctype html>", encoding="utf-8")
        settings = AppSettings(
            project_root=project,
            environment="test",
            database_url=f"sqlite:///{(project / 'queries.db').as_posix()}",
            knowledge_path=project / "knowledge.md",
            admin_index_path=admin_index,
            artifact_output_path=project / "artifacts",
            auth_mode="trusted_headers",
            auth_subject_salt="unit-test-subject-salt",
            log_level="CRITICAL",
        )
        return TestClient(create_admin_app(settings))

    def test_auditor_reads_metrics_snapshot(self):
        reset_metrics()
        with self._make_client() as client:
            response = client.get("/api/admin/metrics", headers=self.headers("auditor", "auditor"))
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertTrue(body["ok"])
            self.assertIn("requests_total", body)
            self.assertIn("request_duration_ms", body)
            self.assertIn("ingestion_queue_depth", body)
            self.assertIn("ingestion_jobs_failed", body)
            self.assertIn("model_usage", body)

    def test_metrics_counts_observed_traffic(self):
        reset_metrics()
        with self._make_client() as client:
            # A 200 read plus a deliberate 404 both flow through the middleware.
            client.get("/api/admin/audit-events", headers=self.headers("auditor", "auditor"))
            client.get("/api/admin/does-not-exist", headers=self.headers("auditor", "auditor"))
            response = client.get("/api/admin/metrics", headers=self.headers("auditor", "auditor"))
            snapshot = response.json()
            # Two requests above are recorded; the metrics call itself is counted after the
            # handler builds the snapshot, so it is not in this reading.
            self.assertGreaterEqual(snapshot["requests_total"], 2)
            self.assertGreaterEqual(snapshot["request_duration_ms"]["samples"], 2)
            self.assertGreaterEqual(snapshot["request_duration_ms"]["samples"], 2)
            self.assertIn("GET /api/admin/audit-events 200", snapshot["requests_by_status"])

    def test_viewer_without_audit_read_is_forbidden(self):
        with self._make_client() as client:
            response = client.get("/api/admin/metrics", headers=self.headers("carol", "viewer"))
            self.assertEqual(response.status_code, 403)

    def test_registry_is_thread_safe_and_monotonic(self):
        reset_metrics()
        m = get_metrics()
        # Direct exercise from multiple threads to assert no lost increments / races.
        import threading

        def hammer():
            for _ in range(250):
                m.inc_request("GET", "/x", 200)
                m.observe_request_duration(1.0)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(m.requests_total, 1000)
        self.assertEqual(m.request_duration.count(), 1000)
