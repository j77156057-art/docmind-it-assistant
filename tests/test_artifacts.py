from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import text

from admin_app import create_admin_app
from backend import AppSettings
from backend.artifacts import ArtifactError, ArtifactService


class ArtifactServiceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="docmind-artifacts-")
        self.service = ArtifactService(Path(self.root.name) / "artifacts")
        self.service.initialize()

    def tearDown(self):
        self.root.cleanup()

    def create(self, artifact_format: str, **extra):
        result = self.service.create({
            "format": artifact_format,
            "filename": f"guide.{artifact_format}",
            "title": "管理员指南",
            "sections": [{"heading": "访问控制", "paragraphs": ["支持 SSO、RBAC 和 ACL。"]}],
            **extra,
        })
        self.assertGreater(result["bytes"], 0)
        self.assertTrue((self.service.output_path / result["filename"]).is_file())
        return result

    def test_creates_and_validates_all_formats(self):
        docx = self.create("docx")
        pdf = self.create("pdf")
        pptx = self.create("pptx")
        xlsx = self.create("xlsx", sheets=[{
            "name": "权限", "headers": ["角色", "能力"], "rows": [["管理员", "管理"]],
        }])
        self.assertGreaterEqual(docx["validation"]["paragraphs"], 2)
        self.assertGreaterEqual(pdf["validation"]["pages"], 1)
        self.assertGreaterEqual(pptx["validation"]["slides"], 2)
        self.assertEqual(xlsx["validation"]["sheets"], ["权限"])

    def test_uses_unique_names_and_lists_newest_first(self):
        first = self.create("docx")
        second = self.create("docx")
        self.assertNotEqual(first["filename"], second["filename"])
        self.assertEqual(self.service.list()[0]["filename"], second["filename"])

    def test_rejects_traversal_and_missing_files(self):
        with self.assertRaises(ArtifactError):
            self.service.create({"format": "pdf", "filename": "../outside.pdf"})
        with self.assertRaises(ArtifactError):
            self.service.resolve("../outside.pdf")

    def test_spreadsheet_escapes_formula_strings(self):
        result = self.create("xlsx", sheets=[{
            "name": "Data", "headers": ["Value"], "rows": [["=HYPERLINK(\"x\")"]],
        }])
        workbook = load_workbook(self.service.output_path / result["filename"], data_only=False)
        self.assertEqual(workbook["Data"]["A2"].value, "'=HYPERLINK(\"x\")")
        workbook.close()


class ArtifactAdminApiTests(unittest.TestCase):
    @staticmethod
    def headers(subject: str, roles: str) -> dict[str, str]:
        return {"X-Auth-Subject": subject, "X-Auth-Roles": roles}

    def test_admin_create_auditor_list_download_and_audit(self):
        with tempfile.TemporaryDirectory(prefix="docmind-artifact-api-") as root:
            project = Path(root)
            knowledge = project / "knowledge.md"
            knowledge.write_text("# IT\n", encoding="utf-8")
            admin_index = project / "admin.html"
            admin_index.write_text("<!doctype html>", encoding="utf-8")
            settings = AppSettings(
                project_root=project,
                environment="test",
                database_url=f"sqlite:///{(project / 'queries.db').as_posix()}",
                knowledge_path=knowledge,
                admin_index_path=admin_index,
                artifact_output_path=project / "artifacts",
                auth_mode="trusted_headers",
                auth_subject_salt="unit-test-subject-salt",
                log_level="CRITICAL",
            )
            application = create_admin_app(settings)
            with TestClient(application) as client:
                payload = {
                    "format": "pdf",
                    "filename": "security-guide.pdf",
                    "title": "安全指南",
                    "sections": [{"heading": "权限", "paragraphs": ["管理员负责授权。"]}],
                }
                forbidden = client.post(
                    "/api/admin/artifacts", json=payload,
                    headers=self.headers("auditor", "auditor"),
                )
                self.assertEqual(forbidden.status_code, 403)

                created = client.post(
                    "/api/admin/artifacts", json=payload,
                    headers={**self.headers("administrator", "admin"),
                             "X-Request-ID": "artifact-create-request"},
                )
                self.assertEqual(created.status_code, 200, created.text)
                item = created.json()
                self.assertEqual(item["validation"]["pages"], 1)

                listed = client.get(
                    "/api/admin/artifacts", headers=self.headers("auditor", "auditor"),
                )
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(listed.json()["items"][0]["filename"], item["filename"])

                downloaded = client.get(
                    item["download_url"], headers=self.headers("auditor", "auditor"),
                )
                self.assertEqual(downloaded.status_code, 200)
                self.assertEqual(downloaded.headers["content-type"], "application/pdf")
                self.assertTrue(downloaded.content.startswith(b"%PDF"))

            database = application.state.database
            with database.engine.connect() as connection:
                audit = connection.execute(text(
                    "SELECT action, target_type, result, request_id "
                    "FROM audit_events ORDER BY id DESC LIMIT 1"
                )).one()
            self.assertEqual(tuple(audit), (
                "artifact_create", "artifact", "success", "artifact-create-request",
            ))

    def test_artifact_download_rejects_untrusted_path(self):
        with tempfile.TemporaryDirectory(prefix="docmind-artifact-api-") as root:
            project = Path(root)
            knowledge = project / "knowledge.md"
            knowledge.write_text("# IT\n", encoding="utf-8")
            settings = AppSettings(
                project_root=project,
                environment="test",
                database_url=f"sqlite:///{(project / 'queries.db').as_posix()}",
                knowledge_path=knowledge,
                artifact_output_path=project / "artifacts",
                auth_mode="trusted_headers",
                auth_subject_salt="unit-test-subject-salt",
                log_level="CRITICAL",
            )
            application = create_admin_app(settings)
            with TestClient(application) as client:
                response = client.get(
                    "/api/admin/artifacts/not-a-document.exe",
                    headers=self.headers("auditor", "auditor"),
                )
                self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
