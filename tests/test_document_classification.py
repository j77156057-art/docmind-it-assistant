"""Document classification as an authorization input.

The property that matters is an asymmetry: a classification may only ever *narrow* who reads a
document. It must not unlock anything on its own, it must survive an ACL that says otherwise,
and it must not be bypassable through the spelling of the value.

`access_scope` plus the document ACL decide *who* a document is shared with; classification adds
a second, independent condition. A confidential document that is also marked public therefore
stays out of reach of a caller without clearance.
"""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from admin_app import create_admin_app
from app import create_app
from backend import (
    AppSettings, EmbeddingClient, GovernanceError, Principal, QueryDatabase,
    allows_classification, is_open_classification, normalize_classification,
)
from ingestion import DocumentIngestionService


CONTENT = "## 财务系统\n财务系统恢复码为内部资料，仅限授权人员。\n"
HANDBOOK = "## 手册\n内部手册。\n"
QUESTION = "财务系统恢复码"

VIEWER = Principal("viewer-1", frozenset({"viewer"}), frozenset())
AUDITOR = Principal("auditor-1", frozenset({"auditor"}), frozenset())
ADMIN = Principal("admin-1", frozenset({"admin"}), frozenset())


class DocumentClassificationTests(unittest.TestCase):
    def make_settings(self, root: str) -> AppSettings:
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
        )

    @staticmethod
    def headers(subject: str, roles: str, groups: str = "") -> dict[str, str]:
        return {"X-Auth-Subject": subject, "X-Auth-Roles": roles, "X-Auth-Groups": groups}

    # -- fixtures --------------------------------------------------------------
    def service(self, root: str):
        database = QueryDatabase(str(Path(root) / "classification.db"))
        database.initialize()
        embeddings = EmbeddingClient(
            mode="hash", provider="builtin", model="hash-1024", base_url="", api_key="",
        )
        ingestion = DocumentIngestionService(
            database, embeddings, max_bytes=1024 * 1024,
            chunk_max_chars=300, chunk_overlap_chars=40,
        )
        return database, embeddings, ingestion

    def indexed_document(self, root: str, *, classification: str, access_scope: str = "public"):
        """Import one document; returns (database, query vector, document_id)."""
        path = Path(root) / "finance.md"
        path.write_text(CONTENT, encoding="utf-8")
        database, embeddings, ingestion = self.service(root)
        imported = ingestion.import_file(
            path, source_key="secret/finance", access_scope=access_scope,
            classification=classification,
        )
        vector = list(embeddings.embed([QUESTION], request_id="classification-test").vectors[0])
        return database, vector, imported["document_id"]

    @staticmethod
    def retrieve(database, vector, principal):
        """Retrieve as this principal, at the clearance the application derives for them."""
        return database.hybrid_search(
            QUESTION, vector, subject_id=principal.subject_id,
            roles=principal.acl_roles, groups=principal.acl_groups,
            allow_confidential=principal.confidential_clearance,
        )

    # -- the rule itself -------------------------------------------------------
    def test_clearance_is_derived_from_role_level(self):
        self.assertFalse(VIEWER.confidential_clearance)
        self.assertTrue(AUDITOR.confidential_clearance)
        self.assertTrue(ADMIN.confidential_clearance)
        # A governance role reads documents through the capability-checked admin surface and never
        # reaches retrieval, so it deliberately carries no clearance.
        editor = Principal("editor-1", frozenset({"knowledge_editor"}), frozenset())
        self.assertFalse(editor.confidential_clearance)

    def test_open_labels_need_no_clearance_and_unknown_ones_fail_closed(self):
        for label in ("public", "internal", "Public", " INTERNAL "):
            self.assertTrue(is_open_classification(label))
            self.assertTrue(allows_classification(label, VIEWER.capabilities))

        for junk in ("机密", "secret", "", None, "confidential"):
            self.assertFalse(is_open_classification(junk))
            self.assertFalse(allows_classification(junk, VIEWER.capabilities))
            # Clearance still reads them: failing closed restricts, it does not lock out.
            self.assertTrue(allows_classification(junk, AUDITOR.capabilities))

        self.assertEqual(normalize_classification(" Confidential "), "confidential")
        with self.assertRaises(ValueError):
            normalize_classification("机密")

    # -- retrieval -------------------------------------------------------------
    def test_classification_narrows_even_a_public_document(self):
        with tempfile.TemporaryDirectory() as root:
            database, vector, _ = self.indexed_document(
                root, classification="confidential", access_scope="public",
            )
            try:
                self.assertEqual(self.retrieve(database, vector, VIEWER), [])
                self.assertTrue(self.retrieve(database, vector, AUDITOR))
                # The lexical path must agree: an embedding outage must not be a way around the
                # classification.
                self.assertEqual(database.lexical_search(
                    QUESTION, subject_id=VIEWER.subject_id, roles=VIEWER.acl_roles,
                    allow_confidential=VIEWER.confidential_clearance,
                ), [])
                self.assertTrue(database.lexical_search(
                    QUESTION, subject_id=AUDITOR.subject_id, roles=AUDITOR.acl_roles,
                    allow_confidential=AUDITOR.confidential_clearance,
                ))
            finally:
                database.dispose()

    def test_clearance_does_not_replace_the_acl(self):
        with tempfile.TemporaryDirectory() as root:
            database, vector, _ = self.indexed_document(
                root, classification="confidential", access_scope="restricted",
            )
            try:
                # Clearance is not a master key: with no ACL entry the document stays closed.
                self.assertEqual(self.retrieve(database, vector, AUDITOR), [])
            finally:
                database.dispose()

    def test_acl_entry_cannot_widen_past_the_classification(self):
        with tempfile.TemporaryDirectory() as root:
            database, vector, document_id = self.indexed_document(
                root, classification="confidential", access_scope="restricted",
            )
            try:
                database.set_document_acl(
                    document_id, [("role", "viewer"), ("user", "auditor-1")],
                    actor_subject_id="admin", request_id="classification-acl",
                    access_scope="restricted",
                )
                # The viewer satisfies the ACL and is still refused: classification is an extra
                # condition, never an alternative one.
                self.assertEqual(self.retrieve(database, vector, VIEWER), [])
                self.assertTrue(self.retrieve(database, vector, AUDITOR))
            finally:
                database.dispose()

    def test_open_classifications_are_unaffected(self):
        for label in ("public", "internal"):
            with tempfile.TemporaryDirectory() as root:
                database, vector, _ = self.indexed_document(
                    root, classification=label, access_scope="public",
                )
                try:
                    self.assertTrue(
                        self.retrieve(database, vector, VIEWER),
                        f"{label} documents must stay readable without clearance",
                    )
                finally:
                    database.dispose()

    def test_unrecognised_stored_label_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            database, vector, document_id = self.indexed_document(
                root, classification="internal", access_scope="public",
            )
            try:
                # Written around the repository boundary on purpose: this is the state an older
                # row or a manual edit could be in, and it must not read as "open".
                with database.engine.connect() as connection:
                    connection.exec_driver_sql(
                        "UPDATE documents SET classification = '机密' WHERE id = ?",
                        (document_id,),
                    )
                    connection.commit()
                self.assertEqual(self.retrieve(database, vector, VIEWER), [])
                self.assertTrue(self.retrieve(database, vector, AUDITOR))
            finally:
                database.dispose()

    def test_overview_outline_withholds_confidential_titles(self):
        with tempfile.TemporaryDirectory() as root:
            database, _, _ = self.indexed_document(
                root, classification="confidential", access_scope="public",
            )
            try:
                # The overview answer is built from titles and headings, which are sensitive on
                # their own even though no chunk content is returned.
                self.assertEqual(database.accessible_document_outline(
                    subject_id=VIEWER.subject_id, roles=VIEWER.acl_roles,
                    allow_confidential=VIEWER.confidential_clearance,
                ), [])
                self.assertTrue(database.accessible_document_outline(
                    subject_id=AUDITOR.subject_id, roles=AUDITOR.acl_roles,
                    allow_confidential=AUDITOR.confidential_clearance,
                ))
            finally:
                database.dispose()

    # -- the import boundary ---------------------------------------------------
    def test_import_cannot_lower_a_classification(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "finance.md"
            path.write_text(CONTENT, encoding="utf-8")
            database, _, ingestion = self.service(root)
            try:
                ingestion.import_file(path, source_key="finance", classification="confidential")
                with self.assertRaises(GovernanceError) as raised:
                    ingestion.import_file(path, source_key="finance", classification="internal")
                self.assertEqual(raised.exception.code, "classification_cannot_be_lowered")
                self.assertEqual(database.list_documents()[0]["classification"], "confidential")

                # Getting stricter is allowed and is the only direction an import may move it.
                other = Path(root) / "handbook.md"
                other.write_text(HANDBOOK, encoding="utf-8")
                ingestion.import_file(other, source_key="handbook", classification="internal")
                ingestion.import_file(other, source_key="handbook", classification="confidential")
                handbook = [row for row in database.list_documents()
                            if row["source_key"] == "handbook"][0]
                self.assertEqual(handbook["classification"], "confidential")
            finally:
                database.dispose()

    def test_import_rejects_an_unknown_classification_with_400(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_admin_app(self.make_settings(root))
            with TestClient(application) as client:
                response = client.post(
                    "/api/admin/documents/import",
                    headers=self.headers("editor", "knowledge_editor"),
                    files={"file": ("finance.md", CONTENT.encode("utf-8"), "text/markdown")},
                    data={"source_key": "finance", "access_scope": "restricted",
                          "classification": "机密"},
                )
            self.assertEqual(response.status_code, 400)
            self.assertIn("密级", response.json()["error"])

    # -- the HTTP surface ------------------------------------------------------
    def test_query_endpoint_serves_confidential_only_to_clearance(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            with TestClient(create_admin_app(settings)) as admin_client:
                imported = admin_client.post(
                    "/api/admin/documents/import",
                    headers=self.headers("administrator", "admin"),
                    files={"file": ("finance.md", CONTENT.encode("utf-8"), "text/markdown")},
                    data={"title": "财务系统", "source_key": "finance",
                          "access_scope": "public", "classification": "confidential"},
                )
            self.assertEqual(imported.status_code, 200)

            with TestClient(create_app(settings)) as client:
                viewer = client.post("/api/query", headers=self.headers("alice", "viewer"), json={
                    "session_id": "viewer-session", "question": QUESTION,
                })
                auditor = client.post("/api/query", headers=self.headers("bob", "auditor"), json={
                    "session_id": "auditor-session", "question": QUESTION,
                })

        self.assertEqual(viewer.status_code, 200)
        self.assertEqual(auditor.status_code, 200)
        # Public by ACL, confidential by classification: only the auditor may be served it.
        viewer_sources = [item["source"] for item in viewer.json()["citations"]]
        auditor_sources = [item["source"] for item in auditor.json()["citations"]]
        self.assertNotIn("财务系统", viewer_sources)
        self.assertIn("财务系统", auditor_sources)


if __name__ == "__main__":
    unittest.main()
