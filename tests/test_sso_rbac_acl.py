from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt
from sqlalchemy import text

from admin_app import create_admin_app
from app import create_app
from backend import AppSettings, OIDCAuthenticator, QueryDatabase
from backend.embeddings import EmbeddingClient
from ingestion import DocumentIngestionService


class SsoRbacAclTests(unittest.TestCase):
    def make_settings(self, root: str, *, auth_mode: str = "trusted_headers") -> AppSettings:
        project = Path(root)
        knowledge = project / "knowledge.md"
        knowledge.write_text("# IT\n## VPN\n请重新登录 VPN。\n", encoding="utf-8")
        web = project / "web" / "index.html"
        web.parent.mkdir(parents=True)
        web.write_text("<!doctype html>", encoding="utf-8")
        return AppSettings(
            project_root=project,
            environment="test",
            database_url=f"sqlite:///{(project / 'queries.db').as_posix()}",
            knowledge_path=knowledge,
            web_index_path=web,
            artifact_output_path=project / "artifacts",
            auth_mode=auth_mode,
            auth_subject_salt="unit-test-subject-salt",
            log_level="CRITICAL",
        )

    @staticmethod
    def headers(subject: str, roles: str, groups: str = "") -> dict[str, str]:
        return {
            "X-Auth-Subject": subject,
            "X-Auth-Roles": roles,
            "X-Auth-Groups": groups,
        }

    def test_oidc_validates_signature_claims_and_normalizes_identity(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        token = jwt.encode({
            "sub": "Alice@example.com",
            "iss": "https://identity.example.com",
            "aud": "docmind",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "realm": {"roles": ["Viewer", "AUDITOR"]},
            "groups": ["IT-Support"],
        }, private_key, algorithm="RS256")
        authenticator = OIDCAuthenticator(
            mode="oidc",
            issuer="https://identity.example.com",
            audience="docmind",
            jwks_url="https://identity.example.com/jwks",
            subject_salt="unit-test-subject-salt",
            role_claim="realm.roles",
            key_resolver=lambda _token: private_key.public_key(),
        )

        principal = authenticator.authenticate({"authorization": f"Bearer {token}"})

        self.assertEqual(len(principal.subject_id), 64)
        self.assertNotIn("Alice@example.com", repr(principal))
        self.assertEqual(principal.roles, frozenset({"viewer", "auditor"}))
        self.assertEqual(principal.groups, frozenset({"it-support"}))
        self.assertTrue(principal.allows("viewer"))
        self.assertTrue(principal.allows("auditor"))
        self.assertFalse(principal.allows("admin"))
        self.assertEqual(principal.acl_roles, ("auditor", "viewer"))

    def test_api_requires_identity_enforces_roles_and_isolates_history(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root))
            with TestClient(application) as client:
                unauthenticated = client.get("/api/runtime/model")
                forbidden = client.get(
                    "/api/usage/ledger", headers=self.headers("alice", "viewer"),
                )
                alice = self.headers("alice", "viewer")
                bob = self.headers("bob", "viewer")
                client.post("/api/query", headers=alice, json={
                    "session_id": "shared", "question": "Alice 的问题",
                })
                client.post("/api/query", headers=bob, json={
                    "session_id": "shared", "question": "Bob 的问题",
                })
                alice_history = client.get(
                    "/api/history?session_id=shared", headers=alice,
                ).json()["items"]
                bob_history = client.get(
                    "/api/history?session_id=shared", headers=bob,
                ).json()["items"]

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual([item["question"] for item in alice_history], ["Alice 的问题"])
        self.assertEqual([item["question"] for item in bob_history], ["Bob 的问题"])

    def test_document_acl_filters_before_retrieval_for_user_group_and_role(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "secret.md"
            path.write_text("## 财务系统\n财务系统恢复码为内部资料。", encoding="utf-8")
            database = QueryDatabase(str(Path(root) / "acl.db"))
            database.initialize()
            embeddings = EmbeddingClient(
                mode="hash", provider="builtin", model="hash-1024", base_url="", api_key="",
            )
            ingestion = DocumentIngestionService(
                database, embeddings, max_bytes=1024 * 1024,
                chunk_max_chars=300, chunk_overlap_chars=40,
            )
            imported = ingestion.import_file(path, source_key="secret/finance")
            vector = list(embeddings.embed(["财务系统恢复码"], request_id="acl-test").vectors[0])

            cases = [
                (("user", "user-1"), {"subject_id": "user-1"}),
                (("group", "finance"), {"subject_id": "user-2", "groups": ("finance",)}),
                (("role", "auditor"), {"subject_id": "user-3", "roles": ("auditor",)}),
            ]
            for index, (entry, identity) in enumerate(cases):
                database.set_document_acl(
                    imported["document_id"], [entry], actor_subject_id="admin",
                    request_id=f"acl-{index}", access_scope="restricted",
                )
                denied = database.hybrid_search(
                    "财务系统恢复码", vector, subject_id="outsider",
                )
                allowed = database.hybrid_search("财务系统恢复码", vector, **identity)
                self.assertEqual(denied, [])
                self.assertTrue(allowed)
            database.dispose()

    def test_admin_acl_endpoint_replaces_acl_and_writes_audit_event(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            database = QueryDatabase(settings.database_url)
            database.initialize()
            document = database.begin_document_import(
                source_key="admin-test", title="Admin test", mime_type="text/plain",
                content_sha256="a" * 64,
            )
            database.dispose()

            application = create_admin_app(settings)
            payload = {
                "access_scope": "restricted",
                "entries": [{"principal_type": "group", "principal_id": "finance"}],
            }
            with TestClient(application) as client:
                denied = client.put(
                    f"/api/admin/documents/{document['document_id']}/acl",
                    headers=self.headers("viewer", "viewer"), json=payload,
                )
                updated = client.put(
                    f"/api/admin/documents/{document['document_id']}/acl",
                    headers={
                        **self.headers("administrator", "admin"),
                        "X-Request-ID": "acl-admin-request",
                    },
                    json=payload,
                )
                visible = client.get(
                    f"/api/admin/documents/{document['document_id']}/acl",
                    headers=self.headers("auditor", "auditor"),
                )

            verification = QueryDatabase(settings.database_url)
            with verification.engine.connect() as connection:
                audit = connection.execute(text(
                    "SELECT action, request_id FROM audit_events ORDER BY id DESC LIMIT 1"
                )).one()
            verification.dispose()

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(visible.json()["entries"], [{
            "principal_type": "group", "principal_id": "finance",
        }])
        self.assertEqual(tuple(audit), ("document_acl_replace", "acl-admin-request"))

    def test_admin_import_is_separate_and_audited(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            with TestClient(application) as client:
                page = client.get("/")
                script = client.get("/assets/admin.js")
                imported = client.post(
                    "/api/admin/documents/import",
                    headers=self.headers("administrator", "admin"),
                    files={"file": ("vpn.md", "## VPN\n请重新登录。".encode(), "text/markdown")},
                    data={
                        "title": "VPN 手册",
                        "source_key": "manual/vpn",
                        "access_scope": "restricted",
                        "classification": "internal",
                    },
                )
                documents = client.get(
                    "/api/admin/documents", headers=self.headers("auditor", "auditor"),
                )
                scope_updated = client.post(
                    "/api/admin/documents/import",
                    headers=self.headers("administrator", "admin"),
                    files={"file": ("vpn.md", "## VPN\n请重新登录。".encode(), "text/markdown")},
                    data={
                        "title": "VPN 手册", "source_key": "manual/vpn",
                        "access_scope": "public", "classification": "internal",
                    },
                )
                updated_documents = client.get(
                    "/api/admin/documents", headers=self.headers("auditor", "auditor"),
                )
                audit = client.get(
                    "/api/admin/audit-events", headers=self.headers("auditor", "auditor"),
                )

        self.assertEqual(page.status_code, 200)
        self.assertIn("DocMind 管理控制台", page.text)
        self.assertIn("default-src 'self'", page.headers["Content-Security-Policy"])
        self.assertEqual(script.status_code, 200)
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()["title"], "VPN 手册")
        self.assertEqual(documents.json()["items"][0]["access_scope"], "restricted")
        self.assertTrue(scope_updated.json()["duplicate"])
        self.assertEqual(updated_documents.json()["items"][0]["access_scope"], "public")
        self.assertEqual(audit.json()["items"][0]["action"], "document_import")

    def test_admin_switches_runtime_model_and_query_service_sees_it(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            query_application = create_app(settings)
            admin_application = create_admin_app(settings)
            viewer = self.headers("viewer", "viewer")
            auditor = self.headers("auditor", "auditor")
            administrator = {
                **self.headers("administrator", "admin"),
                "X-Request-ID": "model-config-request",
            }
            with TestClient(query_application) as query_client, TestClient(admin_application) as admin_client:
                initial = query_client.get("/api/runtime/model", headers=viewer)
                visible = admin_client.get("/api/admin/model-config", headers=auditor)
                denied = admin_client.put(
                    "/api/admin/model-config", headers=auditor,
                    json={"mode": "local", "provider": "ollama", "model": "qwen2.5:7b"},
                )
                invalid = admin_client.put(
                    "/api/admin/model-config", headers=administrator,
                    json={"mode": "cloud", "provider": "qwen", "model": "qwen-plus"},
                )
                updated = admin_client.put(
                    "/api/admin/model-config", headers=administrator,
                    json={"mode": "local", "provider": "ollama", "model": "qwen2.5:7b"},
                )
                dynamic = query_client.get("/api/runtime/model", headers=viewer)
                audit = admin_client.get("/api/admin/audit-events", headers=auditor)

        self.assertEqual(initial.json()["mode"], "knowledge")
        self.assertEqual(visible.status_code, 200)
        self.assertTrue(any(item["key"] == "ollama" for item in visible.json()["providers"]))
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(dynamic.json()["provider"], "ollama")
        self.assertEqual(dynamic.json()["model"], "qwen2.5:7b")
        self.assertEqual(audit.json()["items"][0]["action"], "model_config_update")
        self.assertEqual(audit.json()["items"][0]["request_id"], "model-config-request")


if __name__ == "__main__":
    unittest.main()
