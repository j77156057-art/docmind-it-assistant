"""A-5 stress: concurrent request handling under the metrics middleware.

Uses the read-only ``GET /api/runtime/model`` route (no DB writes, no network) so the test stays
offline and deterministic while still exercising the middleware on many in-flight requests. The
assertion target is that the process never loses a request count and every request completes.
"""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from backend import AppSettings
from backend.metrics import get_metrics, reset_metrics


class ConcurrencyStressTests(unittest.TestCase):
    def test_concurrent_requests_all_succeed_and_metrics_track_them(self):
        reset_metrics()
        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            (project / "knowledge.md").write_text("# IT\n", encoding="utf-8")
            (project / "web").mkdir()
            (project / "web" / "index.html").write_text("<!doctype html>", encoding="utf-8")
            settings = AppSettings(
                project_root=project,
                environment="test",
                database_url=f"sqlite:///{(project / 'queries.db').as_posix()}",
                knowledge_path=project / "knowledge.md",
                web_index_path=project / "web" / "index.html",
                artifact_output_path=project / "artifacts",
                auth_mode="development",
                auth_subject_salt="unit-test-subject-salt",
                log_level="CRITICAL",
            )
            application = create_app(settings)

            concurrency = 8
            per_worker = 16
            total = concurrency * per_worker

            # A single TestClient triggers the lifespan (DB schema) once; reuse it across threads
            # since httpx's underlying transport is safe for concurrent sends.
            with TestClient(application) as client:
                def worker(_: int) -> int:
                    return sum(
                        1 for _ in range(per_worker)
                        if client.get("/api/runtime/model").status_code == 200
                    )

                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    per_client_ok = list(pool.map(worker, range(concurrency)))

            self.assertEqual(per_client_ok, [per_worker] * concurrency)
            # Every request (per_worker per client) is counted exactly once.
            self.assertEqual(get_metrics().requests_total, total)
            self.assertGreaterEqual(
                get_metrics().request_duration.count(), total,
            )
