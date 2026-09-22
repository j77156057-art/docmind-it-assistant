"""Offline end-to-end smoke run of the golden evaluation gate.

Builds a throwaway SQLite project, indexes a few markdown documents through the
real admin HTTP stack, seeds golden questions into ``evaluation_cases``, then
triggers ``EvaluationService.run()`` and prints the metrics.

This exercises the post-change retrieval pipeline end to end:
  * HybridRetriever with the rerank layer (LexicalReranker, offline)
  * parent-child chunking (chunk_child_max_chars populated parent_content)
  * RAGAS-style faithfulness scoring on retrieved context

Run:
    python scripts/smoke_golden_eval.py
No network required (hash embeddings, lexical rerank).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from admin_app import create_admin_app
from backend import AppSettings


PRINTER = (
    "# 打印机手册\n"
    "## 驱动安装\n"
    "zebra printer driver 需要重新安装驱动。\n"
    "## 纸张设置\n"
    "请选择 A4 纸张并重新校准。\n"
)
BADGE = "# 门禁卡\n## 登记流程\nbadge reader 需要重新登记。\n"

# Long enough that chunk_child_max_chars=400 produces child windows with a
# populated parent_content (parent window fills ~1200 chars before splitting).
VPN = (
    "# 企业网络\n"
    "## VPN 凭据过期\n"
    "请在统一身份门户更新您的登录密码，然后断开并重连 VPN 客户端即可恢复访问。"
    "如果更新密码后仍无法连接，请检查本地网络配置，并尝试清除 VPN 客户端本地缓存后重试。"
    "企业 VPN 使用双因子认证，令牌由身份门户统一签发，过期后需重新激活。\n"
    "## 网络配置\n"
    "连接公司 Wi-Fi 需使用 802.1x 企业认证，证书由 IT 部门统一分发并定期轮换。"
    "办公网段与访客网段逻辑隔离，生产系统仅允许从办公网段经由堡垒机访问。"
    "若遇到 DNS 解析异常，请优先切换至备用 DNS 并联系网络运维值班。\n"
    "## 远程接入\n"
    "出差人员可通过零信任网关接入内网，所有流量经策略引擎按身份与设备 posture 评估后放行。"
    "禁止将内网服务端口直接映射至公网，违者按信息安全红线处置。\n"
)


def main() -> int:
    with tempfile.TemporaryDirectory() as root:
        project = Path(root)
        knowledge = project / "knowledge.md"
        knowledge.write_text("# IT\n", encoding="utf-8")
        web = project / "web" / "index.html"
        web.parent.mkdir(parents=True, exist_ok=True)
        web.write_text("<!doctype html>", encoding="utf-8")

        settings = AppSettings(
            project_root=project,
            environment="test",
            database_url=f"sqlite:///{(project / 'queries.db').as_posix()}",
            knowledge_path=knowledge,
            web_index_path=web,
            artifact_output_path=project / "artifacts",
            auth_mode="trusted_headers",
            auth_subject_salt="smoke-subject-salt",
            log_level="CRITICAL",
            governance_mode="direct",
            evaluation_gate_mode="warn",
            evaluation_min_recall=0.8,
            evaluation_min_citation_accuracy=0.9,
            evaluation_max_regression=0.05,
            evaluation_top_k=5,
            # New pipeline knobs under test:
            chunk_child_max_chars=400,
            rerank_enabled=True,
            rerank_mode="lexical",
            rerank_top_n=20,
            evaluation_faithfulness_enabled=True,
            evaluation_min_faithfulness=0.7,
        )

        application = create_admin_app(settings)
        headers = {"X-Auth-Subject": "smoke-1",
                   "X-Auth-Roles": "knowledge_editor,knowledge_reviewer"}

        with TestClient(application) as client:
            # 1) index documents
            for filename, content, key in [
                ("printer.md", PRINTER, "manual/printer"),
                ("badge.md", BADGE, "manual/badge"),
                ("vpn.md", VPN, "handbook/vpn"),
            ]:
                resp = client.post(
                    "/api/admin/documents/import",
                    headers=headers,
                    files={"file": (filename, content.encode("utf-8"), "text/markdown")},
                    data={"source_key": key, "access_scope": "public",
                          "classification": "internal"},
                )
                if resp.status_code != 200:
                    print("IMPORT FAILED", key, resp.status_code, resp.text[:300])
                    return 1
                print(f"indexed {key}: {resp.json().get('status')}")

            # 2) seed golden questions
            cases = [
                {"case_key": "printer-driver", "question": "zebra printer driver",
                 "expected_document_key": "manual/printer", "expected_heading": "驱动安装",
                 "tags": "printer"},
                {"case_key": "printer-wrong-heading", "question": "zebra printer driver",
                 "expected_document_key": "manual/printer", "expected_heading": "网络配置"},
                {"case_key": "vpn-credential", "question": "VPN 凭据过期怎么处理",
                 "expected_document_key": "handbook/vpn", "expected_heading": "vpn 凭据过期"},
                {"case_key": "missing-document", "question": "badge reader",
                 "expected_document_key": "manual/absent"},
                {"case_key": "refusal-kubernetes", "question": "kubernetes 集群迁移",
                 "expect_refusal": True},
            ]
            for case in cases:
                resp = client.put("/api/admin/evaluation/cases", headers=headers, json=case)
                if resp.status_code != 200:
                    print("CASE SAVE FAILED", case["case_key"], resp.status_code, resp.text[:300])
                    return 1
            print(f"seeded {len(cases)} golden cases")

            # 3) trigger the gate (exercises rerank + parent-child + faithfulness)
            created = client.post("/api/admin/evaluation/runs", headers=headers,
                                  json={"trigger": "manual"})
            if created.status_code != 200:
                print("RUN FAILED", created.status_code, created.text[:300])
                return 1
            run_id = created.json()["run"]["run_id"]
            # Per-case results (with per-case faithfulness) live on the detail endpoint;
            # the run-level summary also surfaces the aggregated faithfulness metric.
            detail = client.get(f"/api/admin/evaluation/runs/{run_id}", headers=headers)

        run = detail.json()["run"]
        m = run  # metrics are flattened to top-level keys (recall_at_k, citation_accuracy, ...)
        print("\n================ GOLDEN EVAL SMOKE ================")
        print(f"run_id           : {run_id}")
        print(f"status           : {run['status']}")
        print(f"gate_result      : {run['gate_result']}")
        print(f"gate_reason      : {run['gate_reason']}")
        print(f"total / passed   : {m['total_cases']} / {m['passed_cases']} "
              f"(failed {m['failed_cases']})")
        print(f"recall@k         : {m['recall_at_k']}")
        print(f"citation_acc     : {m['citation_accuracy']}")
        print(f"refusal_acc      : {m['refusal_accuracy']}")
        print(f"faithfulness     : {m['faithfulness']}")
        print(f"faithfulness_cov : {m['faithfulness_coverage']}")
        print("---------------------------------------------------")
        per_case = {it["case_key"]: it for it in run["results"]}
        for key in [c["case_key"] for c in cases]:
            it = per_case[key]
            d = it["detail"]
            print(f"  {key:22} retrieved={it['retrieved']!s:5} "
                  f"rank={it['matched_rank']} citation={it['citation_ok']!s:5} "
                  f"refusal={it['refusal_ok']} faith={d.get('faithfulness')}")
        print("===================================================")

        # Sanity assertions: the harness must reach the production retrieval path
        # (once per answerable case) and the new faithfulness field must be populated.
        assert run["status"] == "succeeded"
        assert m["total_cases"] == len(cases)
        assert any(d.get("faithfulness") is not None for d in (it["detail"] for it in run["results"]))
        print("SMOKE OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
