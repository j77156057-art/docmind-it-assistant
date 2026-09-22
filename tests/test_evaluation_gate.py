"""Evaluation-gate tests: metrics, retrieval reuse, publish blocking and audited override."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from admin_app import create_admin_app
from backend import AppSettings, EvaluationService, QueryDatabase


PRINTER = (
    "# 打印机手册\n## 驱动安装\nzebra printer driver 需要重新安装驱动。\n"
    "## 纸张设置\n请选择 A4 纸张并重新校准。\n"
)
BADGE = "# 门禁卡\n## 登记流程\nbadge reader 需要重新登记。\n"


class EvaluationGateTests(unittest.TestCase):
    def make_settings(self, root: str, *, governance_mode: str = "direct",
                      gate_mode: str = "warn", min_recall: float = 0.8,
                      min_citation: float = 0.5, max_regression: float = 0.05,
                      allow_override: bool = False,
                      eval_allow_override: bool = False) -> AppSettings:
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
            evaluation_gate_mode=gate_mode,
            evaluation_allow_override=eval_allow_override,
            evaluation_min_recall=min_recall,
            evaluation_min_citation_accuracy=min_citation,
            evaluation_max_regression=max_regression,
            evaluation_top_k=5,
        )

    @staticmethod
    def headers(subject: str, roles: str) -> dict[str, str]:
        return {"X-Auth-Subject": subject, "X-Auth-Roles": roles}

    def import_document(self, client: TestClient, headers: dict[str, str], *,
                        filename: str, content: str, source_key: str) -> dict:
        response = client.post(
            "/api/admin/documents/import",
            headers=headers,
            files={"file": (filename, content.encode("utf-8"), "text/markdown")},
            data={"source_key": source_key, "access_scope": "public",
                  "classification": "internal"},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def save_case(self, client: TestClient, headers: dict[str, str], **case) -> dict:
        response = client.put("/api/admin/evaluation/cases", headers=headers, json=case)
        self.assertEqual(response.status_code, 200)
        return response.json()["case"]

    def seed_cases(self, client: TestClient, reviewer: dict[str, str]) -> None:
        self.save_case(
            client, reviewer, case_key="printer-driver", question="zebra printer driver",
            expected_document_key="manual/printer", expected_heading="驱动安装",
            tags="printer",
        )
        self.save_case(
            client, reviewer, case_key="printer-wrong-heading", question="zebra printer driver",
            expected_document_key="manual/printer", expected_heading="网络配置",
        )
        self.save_case(
            client, reviewer, case_key="missing-document", question="badge reader",
            expected_document_key="manual/absent",
        )
        self.save_case(
            client, reviewer, case_key="refusal-kubernetes", question="kubernetes 集群迁移",
            expect_refusal=True,
        )

    def test_run_metrics_and_production_retrieval_reuse(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            reviewer = self.headers("reviewer-1", "knowledge_reviewer")
            database = QueryDatabase(settings.database_url)
            original_hybrid = QueryDatabase.hybrid_search
            calls = {"count": 0}

            def counting_hybrid(self, *args, **kwargs):
                calls["count"] += 1
                return original_hybrid(self, *args, **kwargs)

            try:
                with TestClient(application) as client:
                    self.import_document(client, editor, filename="printer.md",
                                         content=PRINTER, source_key="manual/printer")
                    self.import_document(client, editor, filename="badge.md",
                                         content=BADGE, source_key="manual/badge")
                    self.seed_cases(client, reviewer)

                    QueryDatabase.hybrid_search = counting_hybrid
                    created = client.post(
                        "/api/admin/evaluation/runs", headers=reviewer,
                        json={"trigger": "manual"},
                    )
                    QueryDatabase.hybrid_search = original_hybrid
                    run_id = created.json()["run"]["run_id"]
                    detail = client.get(
                        f"/api/admin/evaluation/runs/{run_id}", headers=reviewer,
                    )
                    cases = client.get("/api/admin/evaluation/cases", headers=reviewer)
            finally:
                QueryDatabase.hybrid_search = original_hybrid
                database.dispose()

            run = created.json()["run"]
            self.assertEqual(created.status_code, 200)
            self.assertEqual(run["status"], "succeeded")
            self.assertEqual(run["total_cases"], 4)
            # The harness must go through the production retrieval path, once per case.
            self.assertEqual(calls["count"], 4)
            # recall/citation share the denominator (cases that should be answered): 2 of 3 hit,
            # and only one of those is cited with the expected heading.
            self.assertEqual(run["recall_at_k"], 0.6667)
            self.assertEqual(run["citation_accuracy"], 0.3333)
            self.assertEqual(run["refusal_accuracy"], 1.0)
            self.assertEqual(run["passed_cases"], 2)
            self.assertLessEqual(run["citation_accuracy"], run["recall_at_k"])

            per_case = {item["case_key"]: item for item in detail.json()["run"]["results"]}
            self.assertEqual(per_case["printer-driver"]["matched_rank"], 1)
            self.assertTrue(per_case["printer-driver"]["citation_ok"])
            self.assertTrue(per_case["printer-wrong-heading"]["retrieved"])
            self.assertFalse(per_case["printer-wrong-heading"]["citation_ok"])
            self.assertFalse(per_case["missing-document"]["retrieved"])
            self.assertTrue(per_case["refusal-kubernetes"]["refusal_ok"])
            # Per-case detail is counters and ranks only: never answer or content text.
            for item in per_case.values():
                self.assertLessEqual(
                    set(item["detail"]),
                    {"hit_count", "expected_rank", "heading_matched", "faithfulness",
                     "citation_ok_strict", "citation_ok_relaxed", "cited_document_rank"},
                )
            self.assertEqual(cases.json()["gate_mode"], "warn")
            self.assertEqual(cases.json()["thresholds"]["min_recall"], 0.8)

    def test_citation_strict_vs_relaxed_diverge(self):
        # The gate used to check only the FIRST matched chunk's heading and break, so a correct
        # document whose leading chunk carried a different heading scored as a miss. Dual-track
        # citation keeps strict (first chunk) and relaxed (any chunk) separate. This test pins the
        # divergence so a future refactor cannot silently collapse the two again.
        from unittest.mock import MagicMock

        class FakeRetriever:
            embeddings = None  # EvaluationService.__init__ reads retriever.embeddings

            def retrieve(self, *args, **kwargs):  # noqa: D401 - deterministic stub
                return [
                    {"source_key": "manual/printer", "heading": "错误章节",
                     "content": "wrong section about zebra driver", "parent_content": "wrong"},
                    {"source_key": "manual/printer", "heading": "正确章节",
                     "content": "right section about zebra driver", "parent_content": "right"},
                ]

        settings = AppSettings(
            environment="test", database_url="sqlite:////tmp/docmind_eval_test.db",
            project_root=Path("."), auth_mode="trusted_headers",
            auth_subject_salt="unit-test-subject-salt", log_level="CRITICAL",
            evaluation_top_k=5,
        )
        service = EvaluationService(settings=settings, database=MagicMock(),
                                     retriever=FakeRetriever())

        later_match = service._evaluate_case({
            "case_id": 1, "question": "zebra printer driver",
            "expected_document_key": "manual/printer", "expected_heading": "正确章节",
            "expect_refusal": False,
        })
        self.assertTrue(later_match["retrieved"])
        self.assertEqual(later_match["cited_document_rank"], 1)
        self.assertFalse(later_match["citation_ok_strict"])
        self.assertTrue(later_match["citation_ok_relaxed"])
        # The expected heading only matches a *later* chunk, so the OLD strict-only gate would
        # have scored this as a citation miss. Dual-track relaxed now passes it; citation_ok (the
        # gate's effective signal) follows relaxed, with citation_ok_strict=False recording that
        # the leading chunk did not match.
        self.assertTrue(later_match["citation_ok"])

        first_match = service._evaluate_case({
            "case_id": 2, "question": "zebra printer driver",
            "expected_document_key": "manual/printer", "expected_heading": "错误章节",
            "expect_refusal": False,
        })
        self.assertTrue(first_match["citation_ok_strict"])
        self.assertTrue(first_match["citation_ok_relaxed"])

        absent = service._evaluate_case({
            "case_id": 3, "question": "anything",
            "expected_document_key": "manual/absent", "expected_heading": "驱动安装",
            "expect_refusal": False,
        })
        self.assertIsNone(absent["cited_document_rank"])
        self.assertFalse(absent["citation_ok_strict"])
        self.assertFalse(absent["citation_ok_relaxed"])

    def seed_review_flow(self, client: TestClient, editor: dict[str, str],
                         reviewer: dict[str, str]) -> tuple[int, int]:
        imported = self.import_document(
            client, editor, filename="printer.md", content=PRINTER, source_key="manual/printer",
        )
        self.import_document(client, editor, filename="badge.md", content=BADGE,
                             source_key="manual/badge")
        self.seed_cases(client, reviewer)
        document_id, version = imported["document_id"], imported["version"]
        approved = client.post(
            f"/api/admin/documents/{document_id}/versions/{version}/review",
            headers=reviewer, json={"decision": "approve", "comment": "内容确认"},
        )
        self.assertEqual(approved.status_code, 200)
        return document_id, version

    def test_block_mode_blocks_publish_and_override_is_audited(self):
        # (1) While the evaluation-override switch is off, nobody may bypass a blocking gate —
        # not the publisher, and not a release manager holding both override capabilities.
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(
                root, governance_mode="review", gate_mode="block", min_recall=1.0,
                min_citation=0.0, eval_allow_override=False,
            )
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            reviewer = self.headers("reviewer-1", "knowledge_reviewer")
            publisher = self.headers("publisher-1", "knowledge_publisher")
            release_manager = self.headers("release-1", "admin knowledge_publisher")
            with TestClient(application) as client:
                document_id, version = self.seed_review_flow(client, editor, reviewer)
                path = f"/api/admin/documents/{document_id}/versions/{version}"
                blocked = client.post(f"{path}/publish", headers=publisher, json={})
                denied_override = client.post(
                    f"{path}/publish", headers=release_manager, json={"override": True},
                )
                documents = client.get("/api/admin/documents", headers=editor)

            self.assertEqual(blocked.status_code, 409)
            self.assertIn("评测门未通过", blocked.json()["error"])
            self.assertEqual(denied_override.status_code, 409)
            statuses = {row["document_id"]: row["status"] for row in documents.json()["items"]}
            self.assertEqual(statuses[document_id], "staged")

        # (2) With the switch on, a release manager's override succeeds and is recorded as such.
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(
                root, governance_mode="review", gate_mode="block", min_recall=1.0,
                min_citation=0.0, eval_allow_override=True,
            )
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            reviewer = self.headers("reviewer-1", "knowledge_reviewer")
            publisher = self.headers("publisher-1", "knowledge_publisher")
            release_manager = self.headers("release-1", "admin knowledge_publisher")
            with TestClient(application) as client:
                document_id, version = self.seed_review_flow(client, editor, reviewer)
                path = f"/api/admin/documents/{document_id}/versions/{version}"
                publisher_override = client.post(
                    f"{path}/publish", headers=publisher, json={"override": True},
                )
                released = client.post(
                    f"{path}/publish", headers=release_manager, json={"override": True},
                )
                reviews = client.get(f"{path}/reviews", headers=reviewer)
                runs = client.get("/api/admin/evaluation/runs", headers=reviewer)

            # A publisher alone still cannot bypass: the override capability is required.
            self.assertEqual(publisher_override.status_code, 409)
            self.assertEqual(released.status_code, 200)
            self.assertEqual(released.json()["version"]["status"], "indexed")
            self.assertEqual(released.json()["evaluation"]["gate_result"], "block")

            actions = [item["action"] for item in reviews.json()["items"]]
            self.assertIn("override_gate", actions)
            override = [item for item in reviews.json()["items"]
                        if item["action"] == "override_gate"][0]
            self.assertTrue(override["is_override"])
            self.assertIn("评测门未通过仍发布", override["comment"])
            # The gate run itself is recorded against the version that was released.
            pre_publish = [run for run in runs.json()["items"] if run["trigger"] == "pre_publish"]
            self.assertTrue(pre_publish)
            self.assertEqual(pre_publish[0]["gate_result"], "block")

    def test_warn_mode_and_empty_golden_set_do_not_block(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(
                root, governance_mode="review", gate_mode="warn", min_recall=1.0,
                min_citation=0.0,
            )
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            reviewer = self.headers("reviewer-1", "knowledge_reviewer")
            publisher = self.headers("publisher-1", "knowledge_publisher")
            with TestClient(application) as client:
                imported = self.import_document(
                    client, editor, filename="printer.md", content=PRINTER,
                    source_key="manual/printer",
                )
                # No golden questions configured yet: the gate warns instead of blocking.
                empty_run = client.post(
                    "/api/admin/evaluation/runs", headers=reviewer, json={"trigger": "manual"},
                )
                path = (
                    f"/api/admin/documents/{imported['document_id']}"
                    f"/versions/{imported['version']}"
                )
                client.post(f"{path}/review", headers=reviewer,
                            json={"decision": "approve", "comment": "ok"})
                self.seed_cases(client, reviewer)
                published = client.post(f"{path}/publish", headers=publisher, json={})

            self.assertEqual(empty_run.json()["run"]["gate_result"], "warn")
            self.assertIn("没有启用的黄金题", empty_run.json()["run"]["gate_reason"])
            self.assertEqual(published.status_code, 200)
            self.assertEqual(published.json()["evaluation"]["gate_result"], "warn")
            self.assertEqual(published.json()["version"]["status"], "indexed")

    def test_regression_against_baseline_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(
                root, gate_mode="warn", min_recall=0.0, min_citation=0.0,
                max_regression=0.1,
            )
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            reviewer = self.headers("reviewer-1", "knowledge_reviewer")
            with TestClient(application) as client:
                self.import_document(client, editor, filename="printer.md", content=PRINTER,
                                     source_key="manual/printer")
                self.import_document(client, editor, filename="badge.md", content=BADGE,
                                     source_key="manual/badge")
                self.save_case(
                    client, reviewer, case_key="printer-driver", question="zebra printer driver",
                    expected_document_key="manual/printer", expected_heading="驱动安装",
                )
                baseline = client.post("/api/admin/evaluation/runs", headers=reviewer,
                                       json={"trigger": "manual"})

                # Point the case at a document that does not exist: recall collapses.
                self.save_case(
                    client, reviewer, case_key="printer-driver", question="zebra printer driver",
                    expected_document_key="manual/absent", expected_heading="驱动安装",
                )
                regressed = client.post("/api/admin/evaluation/runs", headers=reviewer,
                                        json={"trigger": "manual"})

            self.assertEqual(baseline.json()["run"]["gate_result"], "pass")
            self.assertEqual(baseline.json()["run"]["recall_at_k"], 1.0)
            worse = regressed.json()["run"]
            self.assertEqual(worse["recall_at_k"], 0.0)
            self.assertEqual(worse["gate_result"], "warn")
            self.assertIn("回退", worse["gate_reason"])
            self.assertEqual(worse["baseline_run_id"], baseline.json()["run"]["run_id"])

    def test_off_mode_records_no_gate_result(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root, gate_mode="off")
            application = create_admin_app(settings)
            reviewer = self.headers("reviewer-1", "knowledge_reviewer")
            with TestClient(application) as client:
                run = client.post("/api/admin/evaluation/runs", headers=reviewer,
                                  json={"trigger": "manual"})
                me = client.get("/api/me", headers=reviewer)
            self.assertIsNone(run.json()["run"]["gate_result"])
            self.assertIn("关闭", run.json()["run"]["gate_reason"])
            self.assertEqual(me.json()["evaluation_gate_mode"], "off")

    def test_case_management_capabilities_and_delete_protection(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            editor = self.headers("editor-1", "knowledge_editor")
            reviewer = self.headers("reviewer-1", "knowledge_reviewer")
            viewer = {"X-Auth-Subject": "viewer-1", "X-Auth-Roles": "viewer",
                      "X-Auth-Groups": "it-support"}
            with TestClient(application) as client:
                self.import_document(client, editor, filename="printer.md", content=PRINTER,
                                     source_key="manual/printer")
                denied_list = client.get("/api/admin/evaluation/cases", headers=viewer)
                denied_write = client.put(
                    "/api/admin/evaluation/cases", headers=editor,
                    json={"case_key": "x", "question": "y"},
                )
                case = self.save_case(
                    client, reviewer, case_key="printer-driver", question="zebra printer driver",
                    expected_document_key="manual/printer",
                )
                run = client.post("/api/admin/evaluation/runs", headers=reviewer,
                                  json={"trigger": "manual"})
                in_use = client.delete(
                    f"/api/admin/evaluation/cases/{case['case_id']}", headers=reviewer,
                )
                deactivated = client.put(
                    "/api/admin/evaluation/cases", headers=reviewer,
                    json={"case_key": "printer-driver", "question": "zebra printer driver",
                          "expected_document_key": "manual/printer", "active": False},
                )
                after = client.post("/api/admin/evaluation/runs", headers=reviewer,
                                    json={"trigger": "manual"})

            self.assertEqual(denied_list.status_code, 403)
            self.assertEqual(denied_write.status_code, 403)
            self.assertEqual(run.json()["run"]["status"], "succeeded")
            self.assertEqual(in_use.status_code, 409)
            self.assertIn("停用", in_use.json()["error"])
            self.assertFalse(deactivated.json()["case"]["active"])
            # A deactivated case no longer participates in runs.
            self.assertEqual(after.json()["run"]["total_cases"], 0)
            self.assertIn("没有启用的黄金题", after.json()["run"]["gate_reason"])


if __name__ == "__main__":
    unittest.main()
