"""Diagnose *why* citation_ok is False on cases where recall already succeeded.

``citation_ok`` = (top-k contains the expected document) AND (that hit's ``heading``
contains ``expected_heading``). So a case can be retrieved at rank 1 and still fail
citation. This probe dumps the actual top-5 (source_key, heading) for every case so we
can tell whether the miss is:
  (a) wrong document          -> recall problem (embedding/rerank)
  (b) right doc, wrong chunk  -> chunking / heading-granularity problem
  (c) heading empty           -> heading not propagated by the chunker

Offline: hash embeddings only.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from backend import HybridRetriever, QueryDatabase  # noqa: E402
from backend.embeddings import EmbeddingClient  # noqa: E402
from backend.rerank import LexicalReranker  # noqa: E402
from ingestion import DocumentIngestionService  # noqa: E402

GOLDEN = HERE.parent / "golden" / "retrieval_golden_set.json"
OUT = HERE.parent / "tmp_diag_citation.txt"
TOP_K = 5


def main() -> int:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    lines: list[str] = []
    stats = {"wrong_doc": 0, "heading_miss": 0, "heading_empty": 0, "cited": 0, "total": 0}

    for repo in data["repos"]:
        lines.append(f"===== {repo['name']} =====")
        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            db = QueryDatabase(str(project / "k.db"))
            db.initialize()
            emb = EmbeddingClient(mode="hash", provider="builtin", model="hash-1024",
                                  base_url="", api_key="")
            ing = DocumentIngestionService(db, emb, max_bytes=20 * 1024 * 1024,
                                           chunk_max_chars=1200, chunk_overlap_chars=150,
                                           chunk_child_max_chars=400)
            for doc in repo["documents"]:
                p = Path(doc["path"])
                if not p.exists():
                    lines.append(f"  [MISSING DOC] {p}")
                    continue
                ing.import_file(p, source_key=doc["source_key"])
            retriever = HybridRetriever(db, emb, top_k=TOP_K,
                                        reranker=LexicalReranker(), rerank_candidate_limit=20)

            for case in repo["cases"]:
                q = case["question"]
                exp_key = (case.get("expected_document_key") or "").strip()
                exp_head = (case.get("expected_heading") or "").strip().lower()
                hits = retriever.retrieve(q, None, subject_id="diag", roles=(), groups=(),
                                          allow_confidential=True)[:TOP_K]
                rank = None
                got_head = None
                for i, h in enumerate(hits, 1):
                    if (h.get("source_key") or "") == exp_key:
                        rank = i
                        got_head = (h.get("heading") or "").strip()
                        break
                cited = rank is not None and (not exp_head or exp_head in (got_head or "").lower())
                # Is the expected heading actually present somewhere in top-k, just not first?
                later = None
                if not cited and exp_head:
                    for i, h in enumerate(hits, 1):
                        if (h.get("source_key") or "") == exp_key and exp_head in (
                                (h.get("heading") or "").lower()):
                            later = i
                            break
                if later is not None:
                    stats["recoverable_in_topk"] = stats.get("recoverable_in_topk", 0) + 1
                stats["total"] += 1
                if rank is None:
                    stats["wrong_doc"] += 1
                    verdict = "MISS_DOC"
                elif cited:
                    stats["cited"] += 1
                    verdict = "CITED"
                elif not (got_head or "").strip():
                    stats["heading_empty"] += 1
                    verdict = "HEADING_EMPTY"
                else:
                    stats["heading_miss"] += 1
                    verdict = "HEADING_MISS"

                lines.append(f"  {case['case_key']:7} {verdict:13} rank={rank} "
                             f"exp_head=[{exp_head}] got_head=[{(got_head or '')[:60]}]")
                if verdict == "HEADING_MISS":
                    tag = f"  <-- also at rank {later}" if later else ""
                    lines.append(f"      top5: " + " | ".join(
                        f"{(h.get('source_key') or '')}:{(h.get('heading') or '')[:28]}"
                        for h in hits) + tag)
            db.dispose()

    lines.append("")
    lines.append(f"SUMMARY {json.dumps(stats, ensure_ascii=False)}")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"written {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
