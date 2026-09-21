"""Audit export (A-4): CSV/JSON download of audit events with self-auditing.

The auditor (or admin) may pull the audit trail as a file; the export itself is recorded so the
act of exporting the audit log is auditable. Viewers without `audit.read` are refused.
"""
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from admin_app import create_admin_app
from backend import AppSettings


class AuditExportTests(unittest.TestCase):
    @staticmethod
    def headers(subject: str, roles: str) -> dict[str, str]:
        return {"X-Auth-Subject": subject, "X-Auth-Roles": roles}

    def _make_client(self) -> TestClient:
        root = tempfile.mkdtemp(prefix="docmind-audit-export-")
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
        application = create_admin_app(settings)
        return TestClient(application)

    def _seed(self, database, *, actor: str, action: str, target_ref: str,
              request_id: str = "") -> None:
        database.record_audit_event(
            actor_subject_id=actor, action=action, target_type="document",
            target_ref=target_ref, result="success", request_id=request_id,
        )

    def test_auditor_exports_csv_with_header_and_rows(self):
        with self._make_client() as client:
            database = client.app.state.database
            self._seed(database, actor="alice", action="doc_import", target_ref="doc:1")
            self._seed(database, actor="bob", action="model_config_update", target_ref="cfg:1")

            response = client.get(
                "/api/admin/audit-events/export?export_format=csv",
                headers=self.headers("auditor", "auditor"),
            )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
            self.assertIn("attachment; filename=audit-export-", response.headers.get("content-disposition", ""))
            # Strip a possible BOM before parsing.
            text = response.content.decode("utf-8-sig")
            parsed = list(csv.reader(io.StringIO(text)))
            self.assertEqual(parsed[0], ["id", "created_at", "actor_subject_id", "action",
                                         "target_type", "target_ref", "result", "request_id"])
            actions = {r[3] for r in parsed[1:]}
            self.assertEqual(actions, {"doc_import", "model_config_update"})

    def test_auditor_exports_json(self):
        with self._make_client() as client:
            database = client.app.state.database
            self._seed(database, actor="alice", action="doc_import", target_ref="doc:1")

            response = client.get(
                "/api/admin/audit-events/export?export_format=json",
                headers=self.headers("auditor", "auditor"),
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/json")
            payload = json.loads(response.text)
            self.assertIsInstance(payload, list)
            self.assertEqual(payload[0]["action"], "doc_import")

    def test_viewer_without_audit_read_is_forbidden(self):
        with self._make_client() as client:
            forbidden = client.get(
                "/api/admin/audit-events/export",
                headers=self.headers("carol", "viewer"),
            )
            self.assertEqual(forbidden.status_code, 403)

    def test_invalid_format_is_rejected(self):
        with self._make_client() as client:
            bad = client.get(
                "/api/admin/audit-events/export?export_format=xlsx",
                headers=self.headers("auditor", "auditor"),
            )
            self.assertEqual(bad.status_code, 400)

    def test_action_filter_narrows_export(self):
        with self._make_client() as client:
            database = client.app.state.database
            self._seed(database, actor="alice", action="doc_import", target_ref="doc:1")
            self._seed(database, actor="bob", action="model_config_update", target_ref="cfg:1")

            response = client.get(
                "/api/admin/audit-events/export?export_format=csv&action=doc_import",
                headers=self.headers("auditor", "auditor"),
            )
            text = response.content.decode("utf-8-sig")
            parsed = list(csv.reader(io.StringIO(text)))
            actions = {r[3] for r in parsed[1:]}
            self.assertEqual(actions, {"doc_import"})

    def test_date_filter_excludes_out_of_range_events(self):
        with self._make_client() as client:
            database = client.app.state.database
            self._seed(database, actor="alice", action="doc_import", target_ref="doc:1")

            future = client.get(
                "/api/admin/audit-events/export?export_format=csv"
                f"&start=2999-01-01T00:00:00Z",
                headers=self.headers("auditor", "auditor"),
            )
            past = client.get(
                "/api/admin/audit-events/export?export_format=csv"
                f"&end=2000-01-01T00:00:00Z",
                headers=self.headers("auditor", "auditor"),
            )
            unfiltered = client.get(
                "/api/admin/audit-events/export?export_format=csv",
                headers=self.headers("auditor", "auditor"),
            )

            def doc_import_count(response) -> int:
                text = response.content.decode("utf-8-sig")
                parsed = list(csv.reader(io.StringIO(text)))
                return sum(1 for row in parsed[1:] if row[3] == "doc_import")

            # The export self-audits, so the table also holds audit.export rows; we assert only on
            # the seeded event's presence to isolate the date-filter behaviour.
            self.assertEqual(doc_import_count(future), 0)
            self.assertEqual(doc_import_count(past), 0)
            self.assertEqual(doc_import_count(unfiltered), 1)

    def test_export_is_self_audited(self):
        with self._make_client() as client:
            database = client.app.state.database
            self._seed(database, actor="alice", action="doc_import", target_ref="doc:1")

            client.get(
                "/api/admin/audit-events/export?export_format=csv",
                headers=self.headers("auditor", "auditor"),
            )

            with database.engine.connect() as connection:
                row = connection.execute(text(
                    "SELECT action, target_type, target_ref, result "
                    "FROM audit_events WHERE action='audit.export' "
                    "ORDER BY id DESC LIMIT 1"
                )).one()
            self.assertEqual(tuple(row), (
                "audit.export", "audit_export", "format=csv;rows=1", "success",
            ))


if __name__ == "__main__":
    unittest.main()
