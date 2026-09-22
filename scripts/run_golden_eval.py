"""Run the DocMind retrieval golden set against the real admin HTTP stack.

Reads ``golden/retrieval_golden_set.json``, and for each repo:
  1. builds a throwaway project (SQLite by default, or a disposable PostgreSQL database when
     ``IT_TEST_POSTGRES_URL`` is set),
  2. indexes the repo's markdown docs through the real import endpoint
     (unique ``source_key`` per doc; hash embeddings + LexicalReranker, offline),
  3. seeds the repo's golden cases into ``evaluation_cases``,
  4. triggers ``EvaluationService.run()`` and prints recall/citation/refusal/faithfulness.

This exercises the post-change retrieval pipeline end to end:
  * HybridRetriever with the rerank layer (LexicalReranker)
  * parent-child chunking (chunk_child_max_chars → parent_content)
  * RAGAS-style faithfulness scoring on the retrieved (or generated) answer

Run against SQLite (offline, no network required):
    python scripts/run_golden_eval.py

Run against the production PostgreSQL retrieval path (pgvector + tsvector SQL RRF):
    IT_TEST_POSTGRES_URL=postgresql+psycopg://docmind:change-me@127.0.0.1:5432/docmind_it \\
        python scripts/run_golden_eval.py
The script creates and drops its own disposable database, so the server role needs CREATEDB.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # ensure repo root (admin_app, backend) is importable
from admin_app import create_admin_app
from backend import AppSettings

GOLDEN = HERE.parent / "golden" / "retrieval_golden_set.json"

HEADERS = {
    "X-Auth-Subject": "golden-1",
    "X-Auth-Roles": "knowledge_editor,knowledge_reviewer",
}


# --- PostgreSQL baseline support -----------------------------------------------------------
# When IT_TEST_POSTGRES_URL is set the run provisions a disposable database on that server and runs
# the golden set through the *production* retrieval path (pgvector + tsvector SQL RRF), instead of
# the SQLite portable full-table-scan path that `unittest discover` exercises. The pattern mirrors
# tests/test_indexing_graph_postgres.py: create+destroy a throwaway database, migrate it to head.
def _pg_server_url(database_url: str, database: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(database_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


def _pg_connect(database_url: str, *, autocommit: bool = True):
    from psycopg import connect as psycopg_connect
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg_connect(dsn, autocommit=autocommit)


def make_pg_database(postgres_url: str) -> tuple[str, str]:
    """Create a disposable database on the server, migrate to head; return (url, name)."""
    from alembic import command
    from alembic.config import Config
    db_name = f"docmind_golden_{os.getpid()}_{os.urandom(3).hex()}"
    db_url = _pg_server_url(postgres_url, db_name)
    with _pg_connect(_pg_server_url(postgres_url, "postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}"')
    cfg = Config(str(HERE.parent / "alembic.ini"))
    cfg.attributes["database_url"] = db_url
    command.upgrade(cfg, "head")
    return db_url, db_name


def drop_pg_database(postgres_url: str, db_name: str) -> None:
    with _pg_connect(_pg_server_url(postgres_url, "postgres")) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            except Exception:  # noqa: BLE001 - older servers lack WITH (FORCE)
                cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


def detect_embedding_mode(arg_mode: str | None) -> tuple[str, str, str | None]:
    """Resolve the embedding mode for the run.

    Priority: explicit --embedding flag > DASHSCOPE_API_KEY present > offline hash.
    Returns (mode, provider, reason). ``mode`` is 'provider' (real qwen) or 'hash' (offline).
    """
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if arg_mode == "hash":
        return "hash", "builtin", "explicit --embedding hash"
    if arg_mode == "provider":
        if not key:
            return "provider", "qwen", "explicit --embedding provider but DASHSCOPE_API_KEY missing (will fail on import)"
        return "provider", "qwen", f"explicit --embedding provider, key len={len(key)}"
    # auto
    if key:
        return "provider", "qwen", f"auto: DASHSCOPE_API_KEY present (len={len(key)})"
    return "hash", "builtin", "auto: no DASHSCOPE_API_KEY -> offline hash"


def build_settings(project: Path, embedding_mode: str, embedding_provider: str,
                   database_url: str | None = None) -> AppSettings:
    knowledge = project / "knowledge.md"
    knowledge.write_text("# IT\n", encoding="utf-8")
    web = project / "web" / "index.html"
    web.parent.mkdir(parents=True, exist_ok=True)
    web.write_text("<!doctype html>", encoding="utf-8")
    # When using a real provider, inject the API key from the environment into
    # the credentials store (AppSettings.credentials is a plain constructor field).
    credentials: dict[str, "SecretStr"] = {}
    if embedding_mode == "provider":
        from pydantic import SecretStr
        key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if key:
            credentials["DASHSCOPE_API_KEY"] = SecretStr(key)
    return AppSettings(
        project_root=project,
        environment="test",
        # Default to a throwaway SQLite project. When IT_TEST_POSTGRES_URL is set the caller passes
        # a disposable PostgreSQL database URL so the run exercises the production pgvector + tsvector
        # retrieval path (not the SQLite portable full-table-scan path used by `unittest discover`).
        database_url=database_url or f"sqlite:///{(project / 'queries.db').as_posix()}",
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
        # Embedding backend (set per-run):
        embedding_mode=embedding_mode,
        embedding_provider=embedding_provider,
        credentials=credentials,
    )


def run_repo(repo: dict, embedding_mode: str, embedding_provider: str,
             database_url: str | None = None) -> dict:
    with tempfile.TemporaryDirectory() as root:
        project = Path(root)
        settings = build_settings(project, embedding_mode, embedding_provider,
                                  database_url=database_url)
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
            "citation_ok_strict": (it.get("detail") or {}).get("citation_ok_strict"),
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
        "citation_accuracy_strict": m["citation_accuracy_strict"],
        "refusal_accuracy": m["refusal_accuracy"],
        "faithfulness": m["faithfulness"],
        "faithfulness_coverage": m["faithfulness_coverage"],
        "per_case": rows,
    }


def main() -> int:
    arg_mode = None
    out_path = None
    for a in sys.argv[1:]:
        if a.startswith("--embedding"):
            arg_mode = a.split("=", 1)[1] if "=" in a else None
            if arg_mode is None and len(sys.argv) > sys.argv.index(a) + 1:
                arg_mode = sys.argv[sys.argv.index(a) + 1]
        elif a.startswith("--out"):
            out_path = a.split("=", 1)[1] if "=" in a else None
            if out_path is None and len(sys.argv) > sys.argv.index(a) + 1:
                out_path = sys.argv[sys.argv.index(a) + 1]
    embedding_mode, embedding_provider, why = detect_embedding_mode(arg_mode)
    print(f"[embedding] mode={embedding_mode} provider={embedding_provider} ({why})")
    if embedding_mode == "provider" and not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        print("[embedding] WARNING: provider mode requested but DASHSCOPE_API_KEY is empty -> "
              "import will fail with embedding_api_key_missing. Set the env var and retry.")

    postgres_url = os.environ.get("IT_TEST_POSTGRES_URL", "").strip()
    pg_db_url = pg_db_name = None
    if postgres_url:
        print(f"[db] provisioning disposable PostgreSQL database from {postgres_url}")
        pg_db_url, pg_db_name = make_pg_database(postgres_url)

    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    print(f"Loaded golden set: {len(data['repos'])} repos")
    results = []
    try:
        for repo in data["repos"]:
            print(f"\n===== REPO: {repo['name']} ({len(repo['documents'])} docs, "
                  f"{len(repo['cases'])} cases) =====")
            report = run_repo(repo, embedding_mode, embedding_provider, database_url=pg_db_url)
            results.append(report)
            if "error" in report:
                print(f"  ERROR: {report['error']}")
                continue
            print(f"  status           : {report['status']}")
            print(f"  gate_result      : {report['gate_result']}  ({report['gate_reason']})")
            print(f"  total/passed     : {report['total_cases']}/{report['passed_cases']} "
                  f"(failed {report['failed_cases']})")
            print(f"  recall@5         : {report['recall_at_k']}")
            print(f"  citation_acc     : {report['citation_accuracy']} "
                  f"(strict {report.get('citation_accuracy_strict')})")
            print(f"  refusal_acc      : {report['refusal_accuracy']}")
            print(f"  faithfulness     : {report['faithfulness']} "
                  f"(cov {report['faithfulness_coverage']})")
            for r in report["per_case"]:
                print(f"    {r['case_key']:22} ret={str(r['retrieved']):5} "
                      f"rank={str(r['rank']):5} cite={str(r['citation_ok']):5} "
                      f"ref={str(r['refusal_ok']):5} faith={r['faithfulness']}")
    finally:
        if pg_db_name:
            print(f"[db] dropping disposable PostgreSQL database {pg_db_name}")
            drop_pg_database(postgres_url, pg_db_name)

    out = Path(out_path) if out_path else HERE.parent / "tmp_golden_report.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReport written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
