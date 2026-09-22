"""采购硬缺口信号（引用 / 反馈 / 知识缺口 / 组织模型）repo + 管理端点测试。

SQLite 本地全量跑通；真正的 ON DELETE CASCADE / SET NULL 由 CI 在 PostgreSQL 上验证
（unittest.skipUnless(IT_TEST_POSTGRES_URL) 跳过，注明"由 CI 验证"）。

覆盖：
* T3 DB 方法：citations 持久化（chunk/knowledge 两种形态 + best-effort 解析 FK）、feedback 幂等、
  gap 弱引用 + 非 PII 模板、org 同步幂等、healthcheck 含新表。
* T4/T5 接线：/api/feedback 端点（鉴权 + 幂等 + 校验）、/api/query 落库 wiring（mock service）、
  sync_org_on_login。
* T6 管理端点：列表 / 导出（鉴权 403、header、行、自审计、CSV BOM）、resolve/dismiss 改 status、
  org 列表与导出、P1-3 组织导出。
"""
import csv
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from admin_app import create_admin_app
from app import create_app
from assistant import ITQueryService
from backend import AppSettings
from backend.auth import Principal
from backend.database import QueryDatabase
from backend.db_models import (
    DepartmentRecord, DocumentChunkRecord, DocumentRecord, DocumentVersionRecord, GAP_TYPES,
    GroupRecord, KnowledgeGapRecord, QueryCitationRecord, QueryFeedbackRecord, QueryRecord,
    UserDepartmentRecord, UserGroupMembershipRecord, UserRecord, FEEDBACK_RATING,
)

POSTGRES_URL = os.environ.get("IT_TEST_POSTGRES_URL")


def _admin_settings(root: Path) -> AppSettings:
    (root / "knowledge.md").write_text("# IT\n", encoding="utf-8")
    admin_index = root / "admin.html"
    admin_index.write_text("<!doctype html>", encoding="utf-8")
    return AppSettings(
        project_root=root,
        environment="test",
        database_url=f"sqlite:///{(root / 'queries.db').as_posix()}",
        knowledge_path=root / "knowledge.md",
        admin_index_path=admin_index,
        artifact_output_path=root / "artifacts",
        auth_mode="trusted_headers",
        auth_subject_salt="unit-test-subject-salt",
        log_level="CRITICAL",
    )


def _admin_headers(subject: str, roles: str) -> dict[str, str]:
    return {"X-Auth-Subject": subject, "X-Auth-Roles": roles}


class TestProcurementRepository(unittest.TestCase):
    def _db(self) -> QueryDatabase:
        root = Path(tempfile.mkdtemp(prefix="docmind-procurement-"))
        db = QueryDatabase(f"sqlite:///{(root / 'queries.db').as_posix()}")
        db.initialize()
        return db

    def _seed_query(self, db: QueryDatabase, query_id: int = 1) -> int:
        with db._sessions.begin() as session:
            session.add(QueryRecord(
                id=query_id, session_id="s", owner_subject_id="u", question="q",
                evidence="sufficient", model_route="knowledge",
            ))
        return query_id

    def _seed_document_chain(self, db: QueryDatabase) -> tuple[int, int, int]:
        with db._sessions.begin() as session:
            doc = DocumentRecord(
                id=10, source_key="doc/10", title="Doc A", mime_type="text/plain",
                access_scope="restricted", classification="internal",
            )
            session.add(doc)
            version = DocumentVersionRecord(
                id=100, document_id=10, version=1, content_sha256="sha", status="indexed",
            )
            session.add(version)
            chunk = DocumentChunkRecord(
                document_version_id=100, ordinal=1, heading="H", page_number=2,
                content="c", search_text="c", embedding=[0.1, 0.2],
            )
            session.add(chunk)
            session.flush()
        return 10, 100, chunk.id

    # --- 引用持久化：两种形态 + best-effort 解析 FK ----------------------------------------

    def test_record_citations_persists_both_kinds_and_resolves_chunk_fk(self):
        db = self._db()
        self._seed_query(db)
        _doc_id, version_id, chunk_id = self._seed_document_chain(db)

        db.record_citations(1, [
            # chunk 型：source=标题, version=1, chunk=2 -> ordinal 1，应解析出 document_chunk_id
            {"source": "Doc A", "version": 1, "chunk": 2, "section": "H", "page": 2, "score": 0.5},
            # knowledge.md 型：仅有 source/section/line
            {"source": "knowledge.md", "section": "章节一", "line": 7},
        ])

        rows = db.citations()
        self.assertEqual(len(rows), 2)
        by_kind = {r["citation_kind"]: r for r in rows}
        self.assertIn("chunk", by_kind)
        self.assertIn("knowledge", by_kind)

        chunk_row = by_kind["chunk"]
        self.assertEqual(chunk_row["document_chunk_id"], chunk_id)
        self.assertEqual(chunk_row["document_version_id"], version_id)
        self.assertEqual(chunk_row["page_number"], 2)
        self.assertAlmostEqual(chunk_row["score"], 0.5, places=6)
        self.assertIsNone(chunk_row["line_number"])

        knowledge_row = by_kind["knowledge"]
        self.assertIsNone(knowledge_row["document_chunk_id"])
        self.assertEqual(knowledge_row["line_number"], 7)
        self.assertEqual(knowledge_row["source"], "knowledge.md")

    def test_record_citations_empty_is_noop(self):
        db = self._db()
        self._seed_query(db)
        db.record_citations(1, [])
        self.assertEqual(db.citations(), [])

    def test_citations_fk_cascade_declared_in_model(self):
        # 模型层声明：query_citations/query_feedback 对 queries 级联，knowledge_gaps 弱引用 SET NULL
        citation_fk = list(QueryCitationRecord.__table__.c.query_id.foreign_keys)[0]
        self.assertEqual(citation_fk.ondelete, "CASCADE")
        feedback_fk = list(QueryFeedbackRecord.__table__.c.query_id.foreign_keys)[0]
        self.assertEqual(feedback_fk.ondelete, "CASCADE")
        gap_fk = list(KnowledgeGapRecord.__table__.c.query_id.foreign_keys)[0]
        self.assertEqual(gap_fk.ondelete, "SET NULL")
        # CHECK 约束存在且命名稳定
        citation_names = {c.name for c in QueryCitationRecord.__table__.constraints}
        feedback_names = {c.name for c in QueryFeedbackRecord.__table__.constraints}
        self.assertIn("ck_query_citations_kind", citation_names)
        self.assertIn("ck_query_feedback_rating", feedback_names)

    @unittest.skipUnless(POSTGRES_URL, "ON DELETE CASCADE 由 CI 在 PostgreSQL 上验证")
    def test_citations_cascade_delete_query_on_postgres(self):
        db = QueryDatabase(POSTGRES_URL)
        self._seed_query(db)
        db.record_citations(1, [{"source": "knowledge.md", "section": "X", "line": 1}])
        self.assertEqual(len(db.citations()), 1)
        with db._sessions.begin() as session:
            session.delete(session.get(QueryRecord, 1))
        self.assertEqual(len(db.citations()), 0)

    # --- 反馈幂等 ----------------------------------------------------------------

    def test_record_feedback_idempotent(self):
        db = self._db()
        self._seed_query(db)
        db.record_feedback(1, "user-a", "positive", "good")
        db.record_feedback(1, "user-a", "negative", "bad")  # 同 (query, actor) 只更新
        db.record_feedback(1, "user-b", "positive", "ok")    # 不同 actor 独立

        rows = db.feedback()
        self.assertEqual(len(rows), 2)
        a = next(r for r in rows if r["actor_subject_id"] == "user-a")
        b = next(r for r in rows if r["actor_subject_id"] == "user-b")
        self.assertEqual(a["rating"], "negative")  # 更新生效
        self.assertEqual(a["comment"], "bad")
        self.assertEqual(b["rating"], "positive")
        # updated_at 刷新、created_at 保留
        self.assertIsNotNone(a["created_at"])
        self.assertIsNotNone(a["updated_at"])

    def test_record_feedback_rejects_invalid_rating(self):
        db = self._db()
        self._seed_query(db)
        with self.assertRaises(ValueError):
            db.record_feedback(1, "user-a", "meh", "")

    # --- 知识缺口：弱引用 + 非 PII 模板 --------------------------------------------

    def test_register_knowledge_gap_uses_non_pii_template(self):
        db = self._db()
        self._seed_query(db)
        gap_id = db.register_knowledge_gap(1, "insufficient_evidence", "cloud")
        gaps = db.knowledge_gaps()
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual(gap["id"], gap_id)
        self.assertEqual(gap["gap_type"], "insufficient_evidence")
        self.assertEqual(gap["model_route"], "cloud")
        self.assertEqual(gap["status"], "open")
        self.assertIn("自动登记", gap["gap_summary"])
        self.assertIn("cloud", gap["gap_summary"])
        self.assertEqual(gap["query_id"], 1)

    def test_register_knowledge_gap_accepts_explicit_summary(self):
        db = self._db()
        self._seed_query(db)
        db.register_knowledge_gap(1, "insufficient_evidence", "cloud", "运营补充摘要")
        self.assertEqual(db.knowledge_gaps()[0]["gap_summary"], "运营补充摘要")

    def test_knowledge_gap_allows_null_query_id(self):
        db = self._db()
        # 弱引用：query_id 可为 NULL，插入不抛错（源 query 可能已删除）
        with db._sessions.begin() as session:
            session.add(KnowledgeGapRecord(
                query_id=None, gap_type="insufficient_evidence", gap_summary="t",
                model_route="cloud", status="open",
            ))
        self.assertEqual(len(db.knowledge_gaps()), 1)
        self.assertIsNone(db.knowledge_gaps()[0]["query_id"])

    def test_knowledge_gap_status_transitions_and_audits(self):
        db = self._db()
        self._seed_query(db)
        gap_id = db.register_knowledge_gap(1, "insufficient_evidence", "cloud")
        db.resolve_knowledge_gap(gap_id, 55, "admin", "req-1")
        self.assertEqual(db.knowledge_gaps(status="addressed")[0]["status"], "addressed")
        self.assertEqual(db.knowledge_gaps(status="addressed")[0]["resolved_version_id"], 55)
        db.dismiss_knowledge_gap(gap_id, "admin", "req-2")
        self.assertEqual(db.knowledge_gaps()[0]["status"], "dismissed")
        # 自审计通过 audit_events 表验证
        from backend.db_models import AuditEventRecord
        with db._sessions() as session:
            resolve = session.scalar(select(func.count()).select_from(AuditEventRecord).where(
                AuditEventRecord.action == "knowledge_gap.resolve"))
            dismiss = session.scalar(select(func.count()).select_from(AuditEventRecord).where(
                AuditEventRecord.action == "knowledge_gap.dismiss"))
        self.assertEqual(int(resolve or 0), 1)
        self.assertEqual(int(dismiss or 0), 1)

    @unittest.skipUnless(POSTGRES_URL, "ON DELETE SET NULL 由 CI 在 PostgreSQL 上验证")
    def test_knowledge_gap_set_null_on_query_delete(self):
        db = QueryDatabase(POSTGRES_URL)
        self._seed_query(db)
        gap_id = db.register_knowledge_gap(1, "insufficient_evidence", "cloud")
        with db._sessions.begin() as session:
            session.delete(session.get(QueryRecord, 1))
        with db._sessions() as session:
            gap = session.get(KnowledgeGapRecord, gap_id)
            self.assertIsNone(gap.query_id)

    # --- 组织同步幂等 ------------------------------------------------------------

    def test_sync_org_on_login_idempotent(self):
        db = self._db()
        principal = Principal(
            subject_id="sub-1", roles=frozenset(), groups=frozenset({"eng", "it"}),
            display_name="Alice",
        )
        db.sync_org_on_login(principal)
        db.sync_org_on_login(Principal(
            subject_id="sub-1", roles=frozenset(), groups=frozenset({"eng", "it"}),
            display_name="Alice Updated",
        ))
        users = db.org_users()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["display_name"], "Alice Updated")
        self.assertEqual(users[0]["status"], "active")
        groups = db.org_groups()
        self.assertEqual({g["group_key"] for g in groups}, {"eng", "it"})
        for g in groups:
            self.assertEqual(g["member_count"], 1)
        # 成员关系不重复
        with db._sessions() as session:
            memberships = session.scalars(select(UserGroupMembershipRecord)).all()
        self.assertEqual(len(memberships), 2)

    # --- healthcheck 含新表 ------------------------------------------------------

    def test_healthcheck_includes_new_tables(self):
        db = self._db()
        ok, reason = db.healthcheck()
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "ok")


class TestProcurementEndpoints(unittest.TestCase):
    def _client(self) -> TestClient:
        root = Path(tempfile.mkdtemp(prefix="docmind-procurement-ep-"))
        application = create_admin_app(_admin_settings(root))
        return TestClient(application)

    def _db(self, client: TestClient) -> QueryDatabase:
        return client.app.state.database

    def test_auditor_lists_citations_and_viewer_is_forbidden(self):
        with self._client() as client:
            db = self._db(client)
            db.record_citations(1, [{"source": "knowledge.md", "section": "X", "line": 1}])
            forbidden = client.get(
                "/api/admin/citations", headers=_admin_headers("carol", "viewer"))
            self.assertEqual(forbidden.status_code, 403)
            ok = client.get(
                "/api/admin/citations", headers=_admin_headers("auditor", "auditor"))
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertEqual(len(ok.json()["items"]), 1)

    def test_citations_export_csv_header_bom_and_rows(self):
        with self._client() as client:
            db = self._db(client)
            db.record_citations(1, [
                {"source": "Doc A", "version": 1, "chunk": 1, "section": "H", "page": 2, "score": 0.5},
                {"source": "knowledge.md", "section": "X", "line": 3},
            ])
            response = client.get(
                "/api/admin/citations/export?export_format=csv",
                headers=_admin_headers("auditor", "auditor"))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
            self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))  # BOM
            text = response.content.decode("utf-8-sig")
            parsed = list(csv.reader(io.StringIO(text)))
            self.assertEqual(parsed[0], [
                "id", "query_id", "citation_kind", "document_chunk_id", "document_version_id",
                "source", "section", "page_number", "line_number", "score", "citation_rank",
                "created_at",
            ])
            self.assertEqual(len(parsed), 3)  # header + 2 rows
            self.assertEqual({r[2] for r in parsed[1:]}, {"chunk", "knowledge"})

    def test_citations_export_json(self):
        with self._client() as client:
            db = self._db(client)
            db.record_citations(1, [{"source": "knowledge.md", "section": "X", "line": 1}])
            response = client.get(
                "/api/admin/citations/export?export_format=json",
                headers=_admin_headers("auditor", "auditor"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/json")
            payload = json.loads(response.text)
            self.assertIsInstance(payload, list)
            self.assertEqual(payload[0]["citation_kind"], "knowledge")

    def test_export_is_self_audited(self):
        with self._client() as client:
            db = self._db(client)
            db.record_citations(1, [{"source": "knowledge.md", "section": "X", "line": 1}])
            client.get(
                "/api/admin/citations/export?export_format=csv",
                headers=_admin_headers("auditor", "auditor"))
            from backend.db_models import AuditEventRecord
            with db._sessions() as session:
                row = session.execute(select(AuditEventRecord).where(
                    AuditEventRecord.action == "citations.export")).first()
            self.assertIsNotNone(row)
            self.assertEqual(row[0].target_type, "citations_export")

    def test_invalid_export_format_rejected(self):
        with self._client() as client:
            bad = client.get(
                "/api/admin/citations/export?export_format=xlsx",
                headers=_admin_headers("auditor", "auditor"))
            self.assertEqual(bad.status_code, 400)

    def test_knowledge_gap_resolve_and_dismiss_endpoints(self):
        with self._client() as client:
            db = self._db(client)
            gap_id = db.register_knowledge_gap(1, "insufficient_evidence", "cloud")
            # viewer 无 document.write -> 403
            forbidden = client.post(
                f"/api/admin/knowledge-gaps/{gap_id}/resolve",
                json={"resolved_version_id": 9}, headers=_admin_headers("carol", "viewer"))
            self.assertEqual(forbidden.status_code, 403)
            # admin 可 resolve
            ok = client.post(
                f"/api/admin/knowledge-gaps/{gap_id}/resolve",
                json={"resolved_version_id": 9}, headers=_admin_headers("admin", "admin"))
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertEqual(db.knowledge_gaps()[0]["status"], "addressed")
            self.assertEqual(db.knowledge_gaps()[0]["resolved_version_id"], 9)
            # dismiss
            ok2 = client.post(
                f"/api/admin/knowledge-gaps/{gap_id}/dismiss",
                json={}, headers=_admin_headers("admin", "admin"))
            self.assertEqual(ok2.status_code, 200, ok2.text)
            self.assertEqual(db.knowledge_gaps()[0]["status"], "dismissed")
            from backend.db_models import AuditEventRecord
            with db._sessions() as session:
                resolve = session.scalar(select(func.count()).select_from(AuditEventRecord).where(
                    AuditEventRecord.action == "knowledge_gap.resolve"))
                dismiss = session.scalar(select(func.count()).select_from(AuditEventRecord).where(
                    AuditEventRecord.action == "knowledge_gap.dismiss"))
            self.assertEqual(int(resolve or 0), 1)
            self.assertEqual(int(dismiss or 0), 1)

    def test_org_endpoints_list_and_export(self):
        with self._client() as client:
            db = self._db(client)
            db.sync_org_on_login(Principal(
                subject_id="sub-1", roles=frozenset(), groups=frozenset({"eng"}),
                display_name="Alice"))
            # viewer 403
            self.assertEqual(client.get(
                "/api/admin/org/users", headers=_admin_headers("carol", "viewer")).status_code, 403)
            users = client.get(
                "/api/admin/org/users", headers=_admin_headers("auditor", "auditor"))
            self.assertEqual(users.status_code, 200, users.text)
            self.assertEqual(len(users.json()["items"]), 1)
            groups = client.get(
                "/api/admin/org/groups", headers=_admin_headers("auditor", "auditor"))
            self.assertEqual(groups.json()["items"][0]["member_count"], 1)
            depts = client.get(
                "/api/admin/org/departments", headers=_admin_headers("auditor", "auditor"))
            self.assertEqual(depts.status_code, 200)
            # 组织导出（P1-3）
            export = client.get(
                "/api/admin/org/users/export?export_format=csv",
                headers=_admin_headers("auditor", "auditor"))
            self.assertEqual(export.status_code, 200)
            self.assertTrue(export.content.startswith(b"\xef\xbb\xbf"))
            text = export.content.decode("utf-8-sig")
            parsed = list(csv.reader(io.StringIO(text)))
            self.assertEqual(parsed[0], [
                "subject_id", "display_name", "email", "status", "last_seen_at",
                "department_key", "created_at",
            ])


class TestFeedbackEndpoint(unittest.TestCase):
    def _client(self) -> TestClient:
        root = Path(tempfile.mkdtemp(prefix="docmind-feedback-"))
        knowledge = root / "knowledge.md"
        knowledge.write_text("# IT\n", encoding="utf-8")
        web = root / "web" / "index.html"
        web.parent.mkdir(parents=True)
        web.write_text("<!doctype html>", encoding="utf-8")
        settings = AppSettings(
            project_root=root, environment="test",
            database_url=f"sqlite:///{(root / 'queries.db').as_posix()}",
            knowledge_path=knowledge, web_index_path=web,
            artifact_output_path=root / "artifacts", auth_mode="development",
            auth_subject_salt="unit-test-subject-salt", log_level="CRITICAL",
        )
        return TestClient(create_app(settings))

    def test_feedback_endpoint_records_and_is_idempotent(self):
        with self._client() as client:
            database = client.app.state.database
            database.record(  # 真实 query 行（development 主体 subject_id 为 local-development）
                "s", "q", "sufficient", "knowledge", owner_subject_id="local-development")
            ok = client.post("/api/feedback", json={
                "query_id": 1, "rating": "positive", "comment": "good"})
            self.assertEqual(ok.status_code, 200, ok.text)
            # 重复提交同一 (query, actor) -> 仅一行，rating 更新
            ok2 = client.post("/api/feedback", json={
                "query_id": 1, "rating": "negative", "comment": "bad"})
            self.assertEqual(ok2.status_code, 200)
            rows = database.feedback()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["rating"], "negative")
            self.assertEqual(rows[0]["comment"], "bad")

    def test_feedback_endpoint_rejects_invalid_rating(self):
        with self._client() as client:
            bad = client.post("/api/feedback", json={"query_id": 1, "rating": "meh"})
            self.assertEqual(bad.status_code, 400)


class TestQueryWiring(unittest.TestCase):
    def _client(self) -> TestClient:
        root = Path(tempfile.mkdtemp(prefix="docmind-query-wire-"))
        knowledge = root / "knowledge.md"
        knowledge.write_text("# IT\n", encoding="utf-8")
        web = root / "web" / "index.html"
        web.parent.mkdir(parents=True)
        web.write_text("<!doctype html>", encoding="utf-8")
        settings = AppSettings(
            project_root=root, environment="test",
            database_url=f"sqlite:///{(root / 'queries.db').as_posix()}",
            knowledge_path=knowledge, web_index_path=web,
            artifact_output_path=root / "artifacts", auth_mode="development",
            auth_subject_salt="unit-test-subject-salt", log_level="CRITICAL",
        )
        return TestClient(create_app(settings))

    def test_query_persists_citations_and_gap_on_insufficient(self):
        class FakeService:
            def __init__(self, *args, **kwargs):
                pass

            def query(self, session_id, question, principal):
                return {
                    "query_id": 1, "answer": "a", "citations": [
                        {"source": "knowledge.md", "section": "X", "line": 3},
                        {"source": "Doc A", "version": 1, "chunk": 2, "section": "H",
                         "page": 2, "score": 0.5},
                    ], "evidence": "insufficient", "model": {"route": "cloud"}, "usage": None,
                }

        # Patch the module-level reference used by create_app (NOT the shared class
        # object) so we never mutate ITQueryService.__new__ and leak it to other tests.
        with patch("app.ITQueryService", FakeService):
            with self._client() as client:
                database = client.app.state.database
                database.record(
                    "s", "q", "sufficient", "knowledge", owner_subject_id="local-development")
                response = client.post("/api/query", json={"question": "anything"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(database.citations()), 2)
                gaps = database.knowledge_gaps()
                self.assertEqual(len(gaps), 1)
                self.assertEqual(gaps[0]["gap_type"], "insufficient_evidence")
                self.assertEqual(gaps[0]["model_route"], "cloud")
                self.assertEqual(gaps[0]["status"], "open")


if __name__ == "__main__":
    unittest.main()
