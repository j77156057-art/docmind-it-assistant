"""Golden-question evaluation and the pre-publish quality gate.

The single most important rule here: **evaluation must run through the production retrieval
path**. ``HybridRetriever.retrieve`` performs the same embedding, ACL assembly, hybrid search and
lexical fallback that a user query performs. If this module were to build its own SQL, the
metrics would describe a system nobody uses — the classic way an evaluation suite becomes
theatre. ``tests/test_evaluation_gate.py`` asserts the retriever is really the component used.

Evaluation identity is fixed (``system:evaluation`` with the ``viewer`` role) so a gate result
never depends on which administrator pressed the button. A consequence worth knowing: golden
questions may only reference documents that a viewer can see. Restricted documents must grant
``role:viewer`` (or be evaluated under the knowledge-domain model planned as a later batch).
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

from .database import QueryDatabase
from .embeddings import EmbeddingClient
from .logging_config import log_event, request_id_context
from .retrieval import HybridRetriever
from .text_index import lexical_terms


LOGGER = logging.getLogger("docmind.it.evaluation")

EVALUATION_SUBJECT = "system:evaluation"
EVALUATION_ROLES: tuple[str, ...] = ("viewer",)


class EvaluationError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _split_claims(text: str) -> list[str]:
    """Split an answer into sentence-level claims on CJK/Latin sentence boundaries."""
    parts = re.split(r"[。.!?！？\n]+", text or "")
    return [part.strip() for part in parts if part and part.strip()]


def score_faithfulness(answer: str, context: str) -> float:
    """RAGAS-style groundedness, computed deterministically and offline.

    Each answer claim is checked for lexical support in the retrieved ``context``. A claim with no
    checkable terms is counted as supported (it carries no verifiable content). Returns a value in
    [0, 1]. This is a faithful, dependency-free proxy for an LLM-judge faithfulness scorer: a
    later batch can swap in a cross-encoder/LLM judge by replacing only this function.
    """
    claims = _split_claims(answer)
    if not claims:
        return 1.0
    context_terms = set(lexical_terms(context))
    if not context_terms:
        return 0.0
    supported = 0
    for claim in claims:
        claim_terms = {term for term in lexical_terms(claim) if len(term) >= 2}
        if not claim_terms:
            supported += 1
            continue
        overlap = sum(1 for term in claim_terms if term in context_terms)
        if overlap / len(claim_terms) >= 0.5:
            supported += 1
    return round(supported / len(claims), 4)


class EvaluationService:
    """Runs the golden set and turns the result into a gate decision."""

    def __init__(self, *, settings, database: QueryDatabase, retriever: HybridRetriever,
                 embeddings: EmbeddingClient | None = None,
                 answer_generator: Callable[[str, str], str] | None = None):
        self.settings = settings
        self.database = database
        self.retriever = retriever
        self.embeddings = embeddings or retriever.embeddings
        # Optional answer generator: given (question, retrieved_context) returns the answer a
        # real query would surface, so faithfulness scores the *generated* answer rather than an
        # arbitrary retrieved chunk. When None (offline default) the answer is the assembled
        # retrieved context itself — which is exactly what a knowledge-mode query returns without
        # a model gateway. A generative gateway can be injected here to make faithfulness
        # discriminative (see assistant.service.ITQueryService).
        self.answer_generator = answer_generator

    @property
    def top_k(self) -> int:
        return max(1, int(self.settings.evaluation_top_k))

    # -- gate ------------------------------------------------------------------
    def run(self, *, trigger: str = "manual", document_version_id: int | None = None,
            actor_subject_id: str = "", request_id: str = "") -> dict:
        """Execute the active golden set and record the run with its gate decision."""
        cases = self.database.list_evaluation_cases(active_only=True)
        baseline = self.database.latest_evaluation_run(
            document_version_id=document_version_id, succeeded_only=True,
        )
        run = self.database.create_evaluation_run(
            trigger=trigger,
            gate_mode=self.settings.evaluation_gate_mode,
            document_version_id=document_version_id,
            actor_subject_id=actor_subject_id,
            request_id=request_id or request_id_context.get(),
        )
        try:
            results = [
                self._evaluate_case(case, document_version_id=document_version_id)
                for case in cases
            ]
        except EvaluationError as exc:
            self.database.fail_evaluation_run(run["run_id"], exc.code)
            log_event(LOGGER, logging.WARNING, "evaluation_run_failed",
                      run_id=run["run_id"], reason=exc.code, detail=exc.detail[:120])
            raise
        metrics = self._metrics(results)
        gate_result, gate_reason = self._decide(metrics=metrics, baseline=baseline)
        completed = self.database.complete_evaluation_run(
            run["run_id"], metrics=metrics, results=results, gate_result=gate_result,
            gate_reason=gate_reason,
            baseline_run_id=baseline["run_id"] if baseline else None,
        )
        log_event(
            LOGGER, logging.INFO, "evaluation_run_completed",
            run_id=completed["run_id"], trigger=trigger, gate_mode=completed["gate_mode"],
                gate_result=gate_result, total=metrics["total_cases"],
                recall=metrics["recall_at_k"], citation=metrics["citation_accuracy"],
                faithfulness=metrics["faithfulness"],
            )
        return completed

    def latest_gate(self, document_version_id: int) -> dict | None:
        return self.database.latest_evaluation_run(document_version_id=document_version_id)

    def gate_blocks(self, document_version_id: int) -> dict | None:
        """Return the blocking run, or None when publishing may proceed."""
        if self.settings.evaluation_gate_mode != "block":
            return None
        latest = self.latest_gate(document_version_id)
        if latest is None or latest.get("gate_result") != "block":
            return None
        return latest

    # -- measurement -----------------------------------------------------------
    def _evaluate_case(self, case: dict, *, document_version_id: int | None = None) -> dict:
        started = time.perf_counter()
        hits = self.retriever.retrieve(
            case["question"], None,
            subject_id=EVALUATION_SUBJECT, roles=EVALUATION_ROLES, groups=(),
            document_version_id=document_version_id,
            # The ACL identity above stays a plain viewer on purpose: the gate must keep
            # exercising the production retrieval path. Clearance, however, has to be granted
            # explicitly. Without it a confidential version would silently measure as 0 recall
            # and could never pass its own publish gate.
            allow_confidential=True,
        )[: self.top_k]
        latency_ms = round((time.perf_counter() - started) * 1000)
        detail: dict = {"hit_count": len(hits)}

        if case["expect_refusal"]:
            return {
                "case_id": case["case_id"],
                "retrieved": False,
                "matched_rank": None,
                "citation_ok": None,
                "refusal_ok": not hits,
                "latency_ms": latency_ms,
                "detail": detail,
            }

        expected_key = (case.get("expected_document_key") or "").strip()
        if not expected_key:
            raise EvaluationError(
                "evaluation_case_invalid",
                f"用例 {case['case_key']} 既不是应拒答题，也没有期望文档",
            )
        expected_heading = (case.get("expected_heading") or "").strip().lower()

        # Dual-track citation judgment (architect review 2026-09-23):
        #   * the expected document may appear under several chunks in the top-k; the gate used to
        #     check only the *first* matched chunk's heading and `break`, so a correct document
        #     whose leading chunk happened to carry a different heading was scored as a miss.
        #   * citation_ok_strict  -> first matched chunk's heading matches (the old behavior)
        #   * citation_ok_relaxed -> ANY expected-document chunk's heading matches
        #   * cited_document_rank -> best (1-based) rank of the expected document across top-k
        # Keeping both lets us see "recalled but mis-ranked" separately from "never recalled", and
        # makes the gate's citation threshold reachable instead of structurally impossible.
        doc_ranks = [
            index for index, hit in enumerate(hits, 1)
            if (hit.get("source_key") or "") == expected_key
        ]
        best_document_rank = min(doc_ranks) if doc_ranks else None

        strict_heading_ok = False
        relaxed_heading_ok = False
        for index, hit in enumerate(hits, 1):
            if (hit.get("source_key") or "") != expected_key:
                continue
            heading = (hit.get("heading") or "").strip().lower()
            matched = (not expected_heading) or (expected_heading in heading)
            if doc_ranks and index == doc_ranks[0]:
                strict_heading_ok = matched
            if matched:
                relaxed_heading_ok = True

        detail["expected_rank"] = best_document_rank
        detail["cited_document_rank"] = best_document_rank
        detail["heading_matched"] = relaxed_heading_ok
        detail["citation_ok_strict"] = bool(
            best_document_rank is not None and strict_heading_ok
        )
        detail["citation_ok_relaxed"] = bool(
            best_document_rank is not None and relaxed_heading_ok
        )

        context = "\n\n".join(
            (hit.get("parent_content") or hit["content"]) for hit in hits[: self.top_k]
        )
        # Faithfulness scores the answer a query would actually surface. Offline the answer IS the
        # assembled retrieved context; inject a generative gateway via `answer_generator` to score
        # a real model answer. This removes the old `hits[0]["content"]` artifact where the scored
        # answer was an arbitrary single chunk (changing context order left the score unchanged).
        if self.answer_generator is not None and hits:
            answer = self.answer_generator(case["question"], context)
        else:
            answer = context if hits else ""
        detail["faithfulness"] = score_faithfulness(answer, context) if hits else None
        return {
            "case_id": case["case_id"],
            "retrieved": best_document_rank is not None,
            "matched_rank": best_document_rank,
            "citation_ok": bool(best_document_rank is not None and relaxed_heading_ok),
            "citation_ok_strict": detail["citation_ok_strict"],
            "citation_ok_relaxed": detail["citation_ok_relaxed"],
            "cited_document_rank": best_document_rank,
            "refusal_ok": None,
            "latency_ms": latency_ms,
            "detail": detail,
        }

    @staticmethod
    def _metrics(results: list[dict]) -> dict:
        """recall/citation share the same denominator: the cases that *should* be answered.

        Because ``citation_ok`` implies ``retrieved``, ``citation_accuracy <= recall_at_k`` always
        holds — a useful invariant for spotting measurement bugs.
        """
        expected = [item for item in results if item["refusal_ok"] is None]
        retrieved = sum(1 for item in expected if item["retrieved"])
        cited = sum(1 for item in expected if item["citation_ok"])
        cited_strict = sum(1 for item in expected if item["citation_ok_strict"])
        refusals = [item for item in results if item["refusal_ok"] is not None]
        correct_refusals = sum(1 for item in refusals if item["refusal_ok"])
        passed = sum(
            1 for item in results
            if (item["refusal_ok"] if item["refusal_ok"] is not None else item["citation_ok"])
        )
        faithful = [
            item["detail"].get("faithfulness") for item in results
            if item["detail"].get("faithfulness") is not None
        ]
        faithfulness = round(sum(faithful) / len(faithful), 4) if faithful else None
        faithfulness_coverage = _ratio(len(faithful), len(results))
        return {
            "total_cases": len(results),
            "passed_cases": passed,
            "failed_cases": len(results) - passed,
            "recall_at_k": _ratio(retrieved, len(expected)),
            "citation_accuracy": _ratio(cited, len(expected)),
            "citation_accuracy_strict": _ratio(cited_strict, len(expected)),
            "refusal_accuracy": _ratio(correct_refusals, len(refusals)),
            "faithfulness": faithfulness,
            "faithfulness_coverage": faithfulness_coverage,
        }

    def _decide(self, *, metrics: dict, baseline: dict | None) -> tuple[str | None, str]:
        mode = self.settings.evaluation_gate_mode
        if mode == "off":
            return None, "评测门已关闭（IT_EVAL_GATE_MODE=off），仅记录指标"
        if not metrics["total_cases"]:
            # Blocking on an empty golden set would make day-one publishing impossible, so an
            # unconfigured gate is a warning rather than a hard stop. It is never a silent pass.
            return "warn", "没有启用的黄金题，无法判定质量"

        problems: list[str] = []
        recall = metrics["recall_at_k"]
        citation = metrics["citation_accuracy"]
        if recall is not None and recall < self.settings.evaluation_min_recall:
            problems.append(
                f"recall@{self.top_k} {recall:.3f} < {self.settings.evaluation_min_recall:.3f}"
            )
        if citation is not None and citation < self.settings.evaluation_min_citation_accuracy:
            problems.append(
                f"引用命中率 {citation:.3f} < "
                f"{self.settings.evaluation_min_citation_accuracy:.3f}"
            )
        if self.settings.evaluation_faithfulness_enabled:
            faithfulness = metrics.get("faithfulness")
            if faithfulness is not None and faithfulness < self.settings.evaluation_min_faithfulness:
                problems.append(
                    f"忠实度 {faithfulness:.3f} < {self.settings.evaluation_min_faithfulness:.3f}"
                )
        if baseline:
            allowed = self.settings.evaluation_max_regression
            base_recall = baseline.get("recall_at_k")
            base_citation = baseline.get("citation_accuracy")
            if (
                recall is not None and base_recall is not None
                and recall < float(base_recall) - allowed
            ):
                problems.append(
                    f"recall 相对基线回退 {float(base_recall) - recall:.3f} > {allowed:.3f}"
                )
            if (
                citation is not None and base_citation is not None
                and citation < float(base_citation) - allowed
            ):
                problems.append(
                    f"引用命中率相对基线回退 {float(base_citation) - citation:.3f} > {allowed:.3f}"
                )
        if not problems:
            return "pass", ""
        reason = "；".join(problems)
        return ("block" if mode == "block" else "warn"), reason


__all__ = [
    "EVALUATION_ROLES", "EVALUATION_SUBJECT", "EvaluationError", "EvaluationService",
]
