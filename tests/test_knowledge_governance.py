"""Knowledge-governance tests: state machine, duties separation, and retrieval isolation.

The single most important property asserted here is that a version which is not ``indexed``
(because it is staged, rejected or withdrawn) cannot be reached by any retrieval path, even
though its chunks are already stored in the database.
"""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from admin_app import create_admin_app
from backend import AppSettings, Principal, QueryDatabase


STAGED_CONTENT = "## 打印机驱动\nzebra printer driver 需要重新安装。\n"
CHANGED_CONTENT = "## 打印机驱动\nzebra printer driver 需要重新安装。\n补充说明：先拔掉电源。\n"


class KnowledgeGovernanceTests(unittest.TestCase):
    def make_settings(self, root: str, *, governance_mode: str = "review",
                      allow_override: bool = False,
                      require_separation: bool = True) -> AppSettings:
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
            governance_allow_admin_override=allow_override,
            governance_require_separation_of_duties=require_separation,
        )

    @staticmethod
    def headers(subject: str, roles: str, groups: str = "") -> dict[str, str]:
        return {
            "X-Auth-Subject": subject,
            "X-Auth-Roles": roles,
            "X-Auth-Groups": groups,
        }

    def import_document(self, client: TestClient, headers: dict[str, str], *,
                        content: str = STAGED_CONTENT, source_key: str = "manual/printer",
                        filename: str = "printer.md",
                        access_scope: str = "public"):
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

    @staticmethod
    def version_path(document_id: int, version: int, action: str) -> str:
        return f"/api/admin/documents/{document_id}/versions/{version}/{action}"

    def test_review_publish_withdraw_rollback_lifecycle(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor", "it-support")
            reviewer = self.headers("reviewer-1", "knowledge_reviewer", "it-support")
            publisher = self.headers("publisher-1", "knowledge_publisher", "it-support")
            administrator = self.headers("administrator", "admin", "it-support")
            database = QueryDatabase(settings.database_url)
            try:
                with TestClient(application) as client:
                    imported = self.import_document(client, editor)
                    document_id = imported.json()["document_id"]
                    version = imported.json()["version"]

                    pending = client.get("/api/admin/governance/pending", headers=reviewer)
                    preview_denied = client.get(
                        self.version_path(document_id, version, "preview"), headers=editor,
                    )
                    preview = client.get(
                        self.version_path(document_id, version, "preview"), headers=reviewer,
                    )
                    editor_review = client.post(
                        self.version_path(document_id, version, "review"), headers=editor,
                        json={"decision": "approve", "comment": "自审"},
                    )
                    admin_review = client.post(
                        self.version_path(document_id, version, "review"), headers=administrator,
                        json={"decision": "approve", "comment": "越权审核"},
                    )
                    reviewed = client.post(
                        self.version_path(document_id, version, "review"), headers=reviewer,
                        json={"decision": "approve", "comment": "内容确认"},
                    )

                    # Approval authorises publication but must not publish by itself.
                    staged_hits = database.lexical_search(
                        "zebra", subject_id="viewer-1", roles=("viewer",),
                    )
                    editor_publish = client.post(
                        self.version_path(document_id, version, "publish"), headers=editor,
                        json={"comment": "越权发布"},
                    )
                    admin_publish = client.post(
                        self.version_path(document_id, version, "publish"), headers=administrator,
                        json={"comment": "管理员越权发布"},
                    )
                    published = client.post(
                        self.version_path(document_id, version, "publish"), headers=publisher,
                        json={"comment": "发布"},
                    )
                    published_hits = database.lexical_search(
                        "zebra", subject_id="viewer-1", roles=("viewer",),
                    )
                    outline = database.accessible_document_outline(
                        subject_id="viewer-1", roles=("viewer",),
                    )
                    reviews = client.get(
                        self.version_path(document_id, version, "reviews"), headers=reviewer,
                    )

                    republish = client.post(
                        self.version_path(document_id, version, "publish"), headers=publisher,
                        json={},
                    )
                    review_published = client.post(
                        self.version_path(document_id, version, "review"), headers=reviewer,
                        json={"decision": "approve", "comment": "重复审核"},
                    )
                    withdraw_without_reason = client.post(
                        self.version_path(document_id, version, "withdraw"), headers=publisher,
                        json={},
                    )
                    withdrawn = client.post(
                        self.version_path(document_id, version, "withdraw"), headers=publisher,
                        json={"reason": "内容过期"},
                    )
                    withdrawn_hits = database.lexical_search(
                        "zebra", subject_id="viewer-1", roles=("viewer",),
                    )
                    rolled_back = client.post(
                        self.version_path(document_id, version, "rollback"), headers=publisher,
                        json={"reason": "误作废，恢复"},
                    )
                    restored_hits = database.lexical_search(
                        "zebra", subject_id="viewer-1", roles=("viewer",),
                    )
                    rollback_again = client.post(
                        self.version_path(document_id, version, "rollback"), headers=publisher,
                        json={"reason": "重复回滚"},
                    )
                    audit = client.get("/api/admin/audit-events", headers=administrator)
                    me = client.get("/api/me", headers=reviewer)
            finally:
                database.dispose()

        # Import lands in review, not in the retrievable index.
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()["status"], "staged")
        self.assertEqual(imported.json()["governance_mode"], "review")
        self.assertTrue(imported.json()["review_required"])
        self.assertFalse(imported.json()["duplicate"])

        self.assertEqual(pending.status_code, 200)
        self.assertEqual([item["document_id"] for item in pending.json()["items"]], [document_id])

        # Preview needs the review capability and is audited; the editor is refused.
        self.assertEqual(preview_denied.status_code, 403)
        self.assertEqual(preview.status_code, 200)
        self.assertIn("zebra", preview.json()["chunks"][0]["content"])

        # Capability checks: admin no longer inherits review or publish.
        self.assertEqual(editor_review.status_code, 403)
        self.assertEqual(admin_review.status_code, 403)
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.json()["version"]["status"], "staged")

        self.assertEqual(staged_hits, [])
        self.assertEqual(editor_publish.status_code, 403)
        self.assertEqual(admin_publish.status_code, 403)
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()["version"]["status"], "indexed")
        self.assertTrue(published_hits)
        self.assertEqual(outline[0]["title"], "printer")

        actions = [item["action"] for item in reviews.json()["items"]]
        self.assertIn("submit", actions)
        self.assertIn("approve", actions)
        self.assertIn("publish", actions)
        self.assertFalse([item for item in reviews.json()["items"] if item["is_override"]])

        # Illegal transitions are refused instead of silently mutating state.
        self.assertEqual(republish.status_code, 409)
        self.assertEqual(review_published.status_code, 409)
        self.assertEqual(withdraw_without_reason.status_code, 422)

        self.assertEqual(withdrawn.status_code, 200)
        self.assertEqual(withdrawn.json()["version"]["status"], "withdrawn")
        self.assertEqual(withdrawn.json()["version"]["withdrawn_reason"], "内容过期")
        self.assertEqual(withdrawn_hits, [])

        self.assertEqual(rolled_back.status_code, 200)
        self.assertEqual(rolled_back.json()["version"]["status"], "indexed")
        self.assertTrue(restored_hits)
        self.assertEqual(rollback_again.status_code, 409)

        audit_actions = {item["action"] for item in audit.json()["items"]}
        self.assertLessEqual(
            {
                "document_review_approve", "document_publish", "document_withdraw",
                "document_rollback", "document_version_preview",
            },
            audit_actions,
        )
        self.assertIn("document.review", me.json()["capabilities"])
        self.assertNotIn("document.publish", me.json()["capabilities"])

    def test_separation_of_duties_and_audited_override(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root, allow_override=False)
            application = create_admin_app(settings)
            dual = self.headers("dual-1", "admin knowledge_reviewer")
            publisher = self.headers("publisher-1", "knowledge_publisher")
            with TestClient(application) as client:
                imported = self.import_document(client, dual)
                document_id = imported.json()["document_id"]
                version = imported.json()["version"]
                blocked = client.post(
                    self.version_path(document_id, version, "review"), headers=dual,
                    json={"decision": "approve", "comment": "自审"},
                )
                blocked_with_override = client.post(
                    self.version_path(document_id, version, "review"), headers=dual,
                    json={"decision": "approve", "comment": "自审", "override": True},
                )
                publish_without_approval = client.post(
                    self.version_path(document_id, version, "publish"), headers=publisher,
                    json={},
                )
                publisher_review = client.post(
                    self.version_path(document_id, version, "review"), headers=publisher,
                    json={"decision": "approve", "comment": "越权审核"},
                )

        self.assertEqual(blocked.status_code, 403)
        self.assertIn("提交人", blocked.json()["error"])
        # The configuration switch is off, so requesting an override changes nothing.
        self.assertEqual(blocked_with_override.status_code, 403)
        self.assertEqual(publish_without_approval.status_code, 403)
        self.assertIn("审核", publish_without_approval.json()["error"])
        self.assertEqual(publisher_review.status_code, 403)

        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root, allow_override=True)
            application = create_admin_app(settings)
            with TestClient(application) as client:
                imported = self.import_document(client, dual)
                document_id = imported.json()["document_id"]
                version = imported.json()["version"]
                approved = client.post(
                    self.version_path(document_id, version, "review"), headers=dual,
                    json={"decision": "approve", "comment": "单人完成", "override": True},
                )
                reviews = client.get(
                    self.version_path(document_id, version, "reviews"), headers=dual,
                )
                published = client.post(
                    self.version_path(document_id, version, "publish"), headers=publisher,
                    json={"comment": "发布"},
                )

        self.assertEqual(approved.status_code, 200)
        overrides = [item for item in reviews.json()["items"] if item["is_override"]]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["action"], "approve")
        self.assertEqual(overrides[0]["actor_subject_id"],
                         [item for item in reviews.json()["items"]
                          if item["action"] == "submit"][0]["actor_subject_id"])
        self.assertEqual(published.status_code, 200)

    def test_rejection_reopens_same_version_and_changed_content_adds_a_version(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            reviewer = self.headers("reviewer-1", "knowledge_reviewer")
            with TestClient(application) as client:
                imported = self.import_document(client, editor)
                document_id = imported.json()["document_id"]
                reject_without_comment = client.post(
                    self.version_path(document_id, 1, "review"), headers=reviewer,
                    json={"decision": "reject"},
                )
                rejected = client.post(
                    self.version_path(document_id, 1, "review"), headers=reviewer,
                    json={"decision": "reject", "comment": "缺少故障现象"},
                )
                after_reject = client.get("/api/admin/documents", headers=editor)
                reopened = self.import_document(client, editor)
                changed = self.import_document(client, editor, content=CHANGED_CONTENT)
                after_reopen = client.get("/api/admin/documents", headers=editor)
                reviews = client.get(
                    self.version_path(document_id, 1, "reviews"), headers=reviewer,
                )

        self.assertEqual(reject_without_comment.status_code, 400)
        self.assertEqual(rejected.json()["version"]["status"], "rejected")
        self.assertEqual(
            [row["status"] for row in after_reject.json()["items"]
             if row["document_id"] == document_id and row["version"] == 1],
            ["rejected"],
        )

        # uq_document_versions_hash forbids a second row for identical content, so a resubmission
        # reopens the rejected version instead of duplicating it, and still needs a new review.
        self.assertFalse(reopened.json()["duplicate"])
        self.assertTrue(reopened.json()["reopened"])
        self.assertEqual(reopened.json()["version"], 1)
        self.assertEqual(reopened.json()["status"], "staged")
        self.assertEqual(changed.json()["version"], 2)
        self.assertFalse(changed.json()["reopened"])

        statuses = {
            row["version"]: row["status"] for row in after_reopen.json()["items"]
            if row["document_id"] == document_id
        }
        self.assertEqual(statuses, {1: "staged", 2: "staged"})

        actions = [item["action"] for item in reviews.json()["items"]]
        self.assertIn("reopen", actions)
        self.assertIn("reject", actions)

    def test_import_cannot_widen_an_existing_document(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            with TestClient(application) as client:
                created = self.import_document(client, editor, access_scope="restricted")
                escalated = self.import_document(client, editor, access_scope="public")
                documents = client.get("/api/admin/documents", headers=editor)

        self.assertEqual(created.status_code, 200)
        self.assertEqual(escalated.status_code, 403)
        self.assertIn("访问范围", escalated.json()["error"])
        rows = [row for row in documents.json()["items"]
                if row["document_id"] == created.json()["document_id"]]
        self.assertEqual([row["access_scope"] for row in rows], ["restricted"])

    def test_direct_mode_and_capability_model_are_backwards_compatible(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root, governance_mode="direct")
            application = create_admin_app(settings)
            administrator = self.headers("administrator", "admin")
            with TestClient(application) as client:
                imported = self.import_document(client, administrator)
            self.assertEqual(imported.status_code, 200)
            self.assertEqual(imported.json()["status"], "indexed")
            self.assertFalse(imported.json()["review_required"])

        editor = Principal(subject_id="e", roles=frozenset({"knowledge_editor"}),
                           groups=frozenset({"it-support"}))
        reviewer = Principal(subject_id="r", roles=frozenset({"knowledge_reviewer"}),
                             groups=frozenset())
        publisher = Principal(subject_id="p", roles=frozenset({"knowledge_publisher"}),
                              groups=frozenset())
        administrator = Principal(subject_id="a", roles=frozenset({"admin"}), groups=frozenset())

        # Governance roles stay out of ACL resolution: they must not widen document visibility.
        for principal in (editor, reviewer, publisher):
            self.assertEqual(principal.acl_roles, ())
        self.assertEqual(administrator.acl_roles, ("admin", "auditor", "viewer"))

        self.assertTrue(administrator.has_capability("document.write"))
        self.assertFalse(administrator.has_capability("document.review"))
        self.assertFalse(administrator.has_capability("document.publish"))
        self.assertTrue(administrator.has_capability("governance.override"))
        self.assertTrue(reviewer.has_capability("document.review"))
        self.assertFalse(reviewer.has_capability("document.publish"))
        self.assertFalse(reviewer.has_capability("document.write"))
        self.assertTrue(publisher.has_capability("document.publish"))
        self.assertFalse(publisher.has_capability("document.review"))
        self.assertTrue(editor.has_capability("document.write"))
        self.assertFalse(editor.has_capability("document.review"))


if __name__ == "__main__":
    unittest.main()
