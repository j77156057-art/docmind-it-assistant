"""P2 ACL 切换（核心切换 + 部门进 ACL）实现测试。

SQLite 本地全量；PG 专属（真 string_to_array / ON DELETE CASCADE）由 CI 在 PostgreSQL 上验证，
本地无 PG 时相关用例以 skipUnless(IT_TEST_POSTGRES_URL) 跳过，注明"由 CI 验证"。

覆盖：
* T2 解析：_resolve_principal_groups / _resolve_principal_departments
* T3 4 条执行路径 DB 权威：group/department 从组织表解析、role 仍取 claims、claims groups 不再生效
* T4/T5 set_document_acl：department 写入 + principal_id 应用层校验（拒绝悬空、role 放行）
* T6 富化：document_acl 返回 principal_name
* T7 sync_org_on_login：写部门 + oidc_sub、幂等
* T8 部门维护端点：鉴权 403、审计、增删部门/成员、oidc_sub 解析
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from admin_app import create_admin_app
from backend import AppSettings
from backend.auth import Principal
from backend.database import QueryDatabase
from backend.text_index import lexical_text
from backend.db_models import (
    AuditEventRecord, DepartmentRecord, DocumentChunkRecord, DocumentRecord,
    DocumentVersionRecord, GroupRecord, UserDepartmentRecord, UserGroupMembershipRecord,
    UserRecord,
)

POSTGRES_URL = os.environ.get("IT_TEST_POSTGRES_URL")
ROOT = Path(__file__).resolve().parents[1]


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


class TestP2AclSwitchRepository(unittest.TestCase):
    def _db(self) -> QueryDatabase:
        root = Path(tempfile.mkdtemp(prefix="docmind-p2-acl-"))
        db = QueryDatabase(f"sqlite:///{(root / 'queries.db').as_posix()}")
        db.initialize()
        return db

    def _seed_document_chain(self, db, *, access_scope="restricted",
                             classification="internal") -> tuple[int, int, int]:
        with db._sessions.begin() as session:
            doc = DocumentRecord(
                id=10, source_key="doc/10", title="Doc A", mime_type="text/plain",
                access_scope=access_scope, classification=classification,
            )
            session.add(doc)
            version = DocumentVersionRecord(
                id=100, document_id=10, version=1, content_sha256="sha", status="indexed",
            )
            session.add(version)
            chunk = DocumentChunkRecord(
                document_version_id=100, ordinal=1, heading="H", page_number=2,
                content="财务系统恢复码为内部资料。",
                search_text=lexical_text("财务系统恢复码为内部资料。"),
                embedding=[0.1, 0.2],
            )
            session.add(chunk)
            session.flush()
        return 10, 100, chunk.id

    def _seed_principal_org(self, db, subject_id, *, groups=(), departments=()) -> None:
        """Seed a user row plus group/department memberships so resolution is DB-authoritative."""
        with db._sessions.begin() as session:
            session.add(UserRecord(
                subject_id=subject_id, display_name=subject_id, status="active",
                created_at=datetime.now(timezone.utc),
            ))
            for g in groups:
                session.add(GroupRecord(
                    group_key=g, display_name=g, created_at=datetime.now(timezone.utc)))
                session.add(UserGroupMembershipRecord(
                    subject_id=subject_id, group_key=g, created_at=datetime.now(timezone.utc)))
            for d in departments:
                session.add(DepartmentRecord(
                    department_key=d, name=d, created_at=datetime.now(timezone.utc)))
                session.add(UserDepartmentRecord(
                    subject_id=subject_id, department_key=d, created_at=datetime.now(timezone.utc)))

    # --- T2 解析 ---------------------------------------------------------------
    def test_resolve_principal_groups_and_departments(self):
        db = self._db()
        self._seed_principal_org(db, "u1", groups={"eng", "it"}, departments={"dept-a"})
        self.assertEqual(db._resolve_principal_groups("u1"), {"eng", "it"})
        self.assertEqual(db._resolve_principal_departments("u1"), {"dept-a"})
        # 大小写归一 + legacy/未知返回空集
        self.assertEqual(db._resolve_principal_groups("U1"), {"eng", "it"})
        self.assertEqual(db._resolve_principal_groups("legacy"), set())
        self.assertEqual(db._resolve_principal_groups("nobody"), set())
        db.dispose()

    # --- T3 执行路径 DB 权威 ---------------------------------------------------
    def test_group_and_department_acl_resolved_from_db_not_claims(self):
        db = self._db()
        doc_id, _, _ = self._seed_document_chain(db)
        # u2 在 DB 中有 group=eng 与 department=dept-a 成员关系（不是 claim）
        self._seed_principal_org(db, "u2", groups={"eng"}, departments={"dept-a"})
        db.set_document_acl(
            doc_id, [("group", "eng"), ("department", "dept-a"), ("role", "viewer")],
            actor_subject_id="admin", request_id="p2-acl", access_scope="restricted",
        )
        # u2 通过 group 命中（即使没传任何 roles/group claim）
        hits = db.hybrid_search("财务系统恢复码", [0.1, 0.2], subject_id="u2", roles=())
        self.assertTrue(any(h["source_key"] == "doc/10" for h in hits), hits)
        # 单独验证 department 分支
        db.set_document_acl(
            doc_id, [("department", "dept-a")],
            actor_subject_id="admin", request_id="p2-dept", access_scope="restricted",
        )
        dept_hits = db.hybrid_search("财务系统恢复码", [0.1, 0.2], subject_id="u2", roles=())
        self.assertTrue(any(h["source_key"] == "doc/10" for h in dept_hits), dept_hits)
        # 无成员关系的 u3 被拒（无法用 claims 取巧）
        self.assertEqual(db.hybrid_search("财务系统恢复码", [0.1, 0.2], subject_id="u3", roles=()), [])
        # role 仍来自 claims：u4 有 viewer role 但无成员关系 -> role ACL 命中
        db.set_document_acl(
            doc_id, [("role", "viewer")],
            actor_subject_id="admin", request_id="p2-role", access_scope="restricted",
        )
        self.assertTrue(db.hybrid_search("财务系统恢复码", [0.1, 0.2], subject_id="u4", roles=("viewer",)))
        db.dispose()

    def test_lexical_and_outline_paths_resolve_from_db(self):
        db = self._db()
        doc_id, _, _ = self._seed_document_chain(db)
        self._seed_principal_org(db, "u5", departments={"dept-b"})
        db.set_document_acl(
            doc_id, [("department", "dept-b")],
            actor_subject_id="admin", request_id="p2-lex", access_scope="restricted",
        )
        # lexical_search（portable 回退路径）也应经 department 命中。
        # 注：SQLite 下 lexical 走真·词法匹配（按空白分词），故用 search_text 中真实存在的词条「恢复码」。
        lex = db.lexical_search("恢复码", subject_id="u5", roles=())
        self.assertTrue(any(h["source_key"] == "doc/10" for h in lex), lex)
        # accessible_document_outline 经 department 命中
        outline = db.accessible_document_outline(subject_id="u5", roles=())
        self.assertTrue(any(item["title"] == "Doc A" for item in outline), outline)
        db.dispose()

    # --- T4/T5 set_document_acl + principal_id 校验 ----------------------------
    def test_set_document_acl_department_write_and_validation(self):
        db = self._db()
        doc_id, _, _ = self._seed_document_chain(db)
        self._seed_principal_org(db, "u1", groups={"g1"}, departments={"d1"})
        # department 写入成功
        db.set_document_acl(
            doc_id, [("department", "d1"), ("user", "u1"), ("group", "g1"), ("role", "viewer")],
            actor_subject_id="admin", request_id="p2-write", access_scope="restricted",
        )
        types = {row["principal_type"] for row in db.document_acl(doc_id)}
        self.assertEqual(types, {"department", "user", "group", "role"})
        # 悬空 user 被拒
        with self.assertRaises(ValueError):
            db.set_document_acl(doc_id, [("user", "ghost")], actor_subject_id="admin",
                                request_id="x", access_scope="restricted")
        # 悬空 group 被拒
        with self.assertRaises(ValueError):
            db.set_document_acl(doc_id, [("group", "ghost-g")], actor_subject_id="admin",
                                request_id="x", access_scope="restricted")
        # 悬空 department 被拒
        with self.assertRaises(ValueError):
            db.set_document_acl(doc_id, [("department", "ghost-d")], actor_subject_id="admin",
                                request_id="x", access_scope="restricted")
        # role 不校验（无表），悬空 role 放行
        db.set_document_acl(doc_id, [("role", "phantom")], actor_subject_id="admin",
                            request_id="x", access_scope="restricted")
        db.dispose()

    # --- T6 富化 ---------------------------------------------------------------
    def test_document_acl_enrichment_principal_name(self):
        db = self._db()
        doc_id, _, _ = self._seed_document_chain(db)
        self._seed_principal_org(db, "u1", groups={"g1"}, departments={"d1"})
        db.set_document_acl(
            doc_id, [("user", "u1"), ("group", "g1"), ("department", "d1"), ("role", "admin")],
            actor_subject_id="admin", request_id="p2-enrich", access_scope="restricted",
        )
        entries = {e["principal_id"]: e for e in db.document_acl(doc_id)}
        self.assertEqual(entries["u1"]["principal_name"], "u1")
        self.assertEqual(entries["g1"]["principal_name"], "g1")
        self.assertEqual(entries["d1"]["principal_name"], "d1")
        # role 无组织表 -> 回退 principal_id
        self.assertEqual(entries["admin"]["principal_name"], "admin")
        # document_access 自动继承 principal_name
        self.assertIn("principal_name", db.document_access(doc_id)["entries"][0])
        db.dispose()

    # --- T7 sync_org_on_login 写部门 + oidc_sub ---------------------------------
    def test_sync_org_on_login_writes_departments_and_oidc_sub(self):
        db = self._db()
        principal = Principal(
            subject_id="sub-x", roles=frozenset(), groups=frozenset(),
            display_name="X", departments=frozenset({"hr"}), oidc_sub="sub-raw-123",
        )
        db.sync_org_on_login(principal)
        with db._sessions() as session:
            depts = session.scalars(select(UserDepartmentRecord)).all()
            user = session.get(UserRecord, "sub-x")
        self.assertEqual({d.department_key for d in depts}, {"hr"})
        self.assertEqual(user.oidc_sub, "sub-raw-123")
        # 二次登录幂等：不重复建成员、不覆盖 oidc_sub
        db.sync_org_on_login(principal)
        with db._sessions() as session:
            depts = session.scalars(select(UserDepartmentRecord)).all()
            user = session.get(UserRecord, "sub-x")
        self.assertEqual(len(depts), 1)
        self.assertEqual(user.oidc_sub, "sub-raw-123")
        db.dispose()

    def test_sync_org_on_login_non_oidc_leaves_departments_and_oidc_sub_empty(self):
        db = self._db()
        principal = Principal(
            subject_id="sub-y", roles=frozenset(), groups=frozenset(), display_name="Y",
        )
        db.sync_org_on_login(principal)
        with db._sessions() as session:
            depts = session.scalars(select(UserDepartmentRecord)).all()
            user = session.get(UserRecord, "sub-y")
        self.assertEqual(depts, [])
        self.assertIsNone(user.oidc_sub)
        db.dispose()


class TestP2DepartmentEndpoints(unittest.TestCase):
    def _client(self):
        root = Path(tempfile.mkdtemp(prefix="docmind-p2-ep-"))
        application = create_admin_app(_admin_settings(root))
        application.state.database.initialize()
        return TestClient(application), root

    def _db(self, client):
        return client.app.state.database

    def test_department_endpoints_require_acl_write_and_audit(self):
        client, _ = self._client()
        db = self._db(client)
        # viewer 无 acl.write -> 403
        self.assertEqual(client.post(
            "/api/admin/org/departments",
            json={"department_key": "eng", "name": "Engineering"},
            headers=_admin_headers("carol", "viewer"),
        ).status_code, 403)
        # admin 建部门
        created = client.post(
            "/api/admin/org/departments",
            json={"department_key": "eng", "name": "Engineering"},
            headers=_admin_headers("admin", "admin"),
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["department_key"], "eng")
        # 加成员（subject_id 优先）
        db.sync_org_on_login(Principal(
            subject_id="member-1", roles=frozenset(), groups=frozenset(), display_name="M1",
            oidc_sub="oidc-m1",
        ))
        added = client.post(
            "/api/admin/org/departments/eng/members",
            json={"subject_id": "member-1"},
            headers=_admin_headers("admin", "admin"),
        )
        self.assertEqual(added.status_code, 200, added.text)
        # oidc_sub 解析加成员（已存在 -> 幂等 upsert 仍 200）
        added2 = client.post(
            "/api/admin/org/departments/eng/members",
            json={"oidc_sub": "oidc-m1"},
            headers=_admin_headers("admin", "admin"),
        )
        self.assertEqual(added2.status_code, 200, added2.text)
        # 成员列表含 member-1（audit.read 用 auditor）
        members = client.get(
            "/api/admin/org/users",
            headers=_admin_headers("auditor", "auditor"),
        ).json()["items"]
        eng_members = [u for u in members if u["department_key"] == "eng"]
        self.assertEqual(len(eng_members), 1)
        # 删成员
        removed = client.delete(
            "/api/admin/org/departments/eng/members/member-1",
            headers=_admin_headers("admin", "admin"),
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        # 删部门（级联/显式清理成员）
        deleted = client.delete(
            "/api/admin/org/departments/eng",
            headers=_admin_headers("admin", "admin"),
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        with db._sessions() as session:
            remaining = session.scalars(select(UserDepartmentRecord)).all()
            dept = session.get(DepartmentRecord, "eng")
        self.assertEqual(remaining, [])
        self.assertIsNone(dept)
        # 审计：org_department.* 行存在
        with db._sessions() as session:
            actions = [r[0] for r in session.execute(select(AuditEventRecord.action)).all()]
        self.assertIn("org_department.create", actions)
        self.assertIn("org_department.member_add", actions)
        self.assertIn("org_department.member_remove", actions)
        self.assertIn("org_department.delete", actions)

    def test_department_member_missing_oidc_sub_or_department_rejected(self):
        client, _ = self._client()
        db = self._db(client)
        db.sync_org_on_login(Principal(
            subject_id="member-2", roles=frozenset(), groups=frozenset(), display_name="M2",
        ))
        # 查不到的 oidc_sub -> 400
        bad = client.post(
            "/api/admin/org/departments/eng/members",
            json={"oidc_sub": "does-not-exist"},
            headers=_admin_headers("admin", "admin"),
        )
        self.assertEqual(bad.status_code, 400, bad.text)
        # 部门不存在 -> 400
        bad_dept = client.post(
            "/api/admin/org/departments/nope/members",
            json={"subject_id": "member-2"},
            headers=_admin_headers("admin", "admin"),
        )
        self.assertEqual(bad_dept.status_code, 400, bad_dept.text)


if __name__ == "__main__":
    unittest.main()
