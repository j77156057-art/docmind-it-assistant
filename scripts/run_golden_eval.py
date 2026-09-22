"""Run the DocMind retrieval golden set against the real admin HTTP stack.

Reads ``golden/retrieval_golden_set.json``, and for each repo:
  1. builds a throwaway SQLite project,
  2. indexes the repo's markdown docs through the real import endpoint
     (unique ``source_key`` per doc; hash embeddings + LexicalReranker, offline),
  3. seeds the repo's golden cases into ``evaluation_cases``,
  4. triggers ``EvaluationService.run()`` and prints recall/citation/refusal/faithfulness.

This exercises the post-change retrieval pipeline end to end:
  * HybridRetriever with the rerank layer (LexicalReranker)
  * parent-child chunking (chunk_child_max_chars → parent_content)
  * RAGAS-style faithfulness scoring on retrieved context

Run:
    python scripts/run_golden_eval.py
No network required (hash embeddings, lexical rerank).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from admin_app import create_admin_app
from backend import AppSettings

HERE = Path(__file__).resolve().parent
GOLDEN = HERE.parent / "golden" / "retrieval_golden_set.json"

HEADERS = {
    "X-Auth-Subject": "golden-1",
    "X-Auth-Roles": "knowledge_editor,knowledge_reviewer",
}


def build_settings(project: Path) -> AppSettings:
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
        auth_subject_salt="golden-subject-salt",
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


def run_repo(repo: dict) -> dict:
    with tempfile.TemporaryDirectory() as root:
        project = Path(root)
        settings = build_settings(project)
        application = create_admin_app(settings)
        with TestClient(application) as client:
            # 1) index documents
            indexed = []
            for doc in repo["documents"]:
                path = Path(doc["path"])
                if not path.exists():
                    print(f"  [WARN] missing doc: {path}")
                    continue
                resp = client.post(
                    "/api/admin/documents/import",
                    headers=HEADERS,
                    files={"file": (Path(doc["source_key"]).name + ".md",
                                   path.read_bytes(), "text/markdown")},
                    data={"source_key": doc["source_key"], "access_scope": "public",
                          "classification": "internal"},
                )
                if resp.status_code != 200:
                    print(f"  [IMPORT FAIL] {doc['source_key']} {resp.status_code} {resp.text[:200]}")
                    return {"repo": repo["name"], "error": f"import {doc['source_key']}"}
                indexed.append(doc["source_key"])
            print(f"  indexed {len(indexed)} docs: {indexed}")

            # 2) seed golden cases
            seeded = 0
            for case in repo["cases"]:
                payload = dict(case)
                payload.setdefault("expect_refusal", False)
                payload.setdefault("active", True)
                resp = client.put("/api/admin/evaluation/cases", headers=HEADERS, json=payload)
                if resp.status_code != 200:
                    print(f"  [CASE FAIL] {case['case_key']} {resp.status_code} {resp.text[:200]}")
                    return {"repo": repo["name"], "error": f"case {case['case_key']}"}
                seeded += 1
            print(f"  seeded {seeded} cases")

            # 3) trigger the gate
            created = client.post("/api/admin/evaluation/runs", headers=HEADERS,
                                 json={"trigger": "manual"})
            if created.status_code != 200:
                print(f"  [RUN FAIL] {created.status_code} {created.text[:200]}")
                return {"repo": repo["name"], "error": "run"}
            run_id = created.json()["run"]["run_id"]
            detail = client.get(f"/api/admin/evaluation/runs/{run_id}", headers=HEADERS)
            run = detail.json()["run"]

    m = run
    per_case = {it["case_key"]: it for it in run["results"]}
    rows = []
    for case in repo["cases"]:
        it = per_case.get(case["case_key"], {})
        d = it.get("detail", {})
        rows.append({
            "case_key": case["case_key"],
            "retrieved": it.get("retrieved"),
            "rank": it.get("matched_rank"),
            "citation_ok": it.get("citation_ok"),
            "refusal_ok": it.get("refusal_ok"),
            "faithfulness": d.get("faithfulness"),
        })

    return {
        "repo": repo["name"],
        "run_id": run_id,
        "status": m["status"],
        "gate_result": m["gate_result"],
        "gate_reason": m["gate_reason"],
        "total_cases": m["total_cases"],
        "passed_cases": m["passed_cases"],
        "failed_cases": m["failed_cases"],
        "recall_at_k": m["recall_at_k"],
        "citation_accuracy": m["citation_accuracy"],
        "refusal_accuracy": m["refusal_accuracy"],
        "faithfulness": m["faithfulness"],
        "faithfulness_coverage": m["faithfulness_coverage"],
        "per_case": rows,
    }


def main() -> int:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    print(f"Loaded golden set: {len(data['repos'])} repos")
    results = []
    for repo in data["repos"]:
        print(f"\n===== REPO: {repo['name']} ({len(repo['documents'])} docs, "
              f"{len(repo['cases'])} cases) =====")
        report = run_repo(repo)
        results.append(report)
        if "error" in report:
            print(f"  ERROR: {report['error']}")
            continue
        print(f"  status           : {report['status']}")
        print(f"  gate_result      : {report['gate_result']}  ({report['gate_reason']})")
        print(f"  total/passed     : {report['total_cases']}/{report['passed_cases']} "
              f"(failed {report['failed_cases']})")
        print(f"  recall@5         : {report['recall_at_k']}")
        print(f"  citation_acc     : {report['citation_accuracy']}")
        print(f"  refusal_acc      : {report['refusal_accuracy']}")
        print(f"  faithfulness     : {report['faithfulness']} "
              f"(cov {report['faithfulness_coverage']})")
        for r in report["per_case"]:
            print(f"    {r['case_key']:22} ret={str(r['retrieved']):5} "
                  f"rank={str(r['rank']):5} cite={str(r['citation_ok']):5} "
                  f"ref={str(r['refusal_ok']):5} faith={r['faithfulness']}")

    out = HERE.parent / "tmp_golden_report.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReport written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
