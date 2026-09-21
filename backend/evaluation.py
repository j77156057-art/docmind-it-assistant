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
import time

from .database import QueryDatabase
from .embeddings import EmbeddingClient
from .logging_config import log_event, request_id_context
from .retrieval import HybridRetriever


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


class EvaluationService:
    """Runs the golden set and turns the result into a gate decision."""

    def __init__(self, *, settings, database: QueryDatabase, retriever: HybridRetriever,
                 embeddings: EmbeddingClient | None = None):
        self.settings = settings
        self.database = database
        self.retriever = retriever
        self.embeddings = embeddings or retriever.embeddings

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
        rank = None
        heading_matched = False
        for index, hit in enumerate(hits, 1):
            if (hit.get("source_key") or "") != expected_key:
                continue
            rank = index
            heading = (hit.get("heading") or "").strip().lower()
            heading_matched = (not expected_heading) or (expected_heading in heading)
            break
        detail["expected_rank"] = rank
        detail["heading_matched"] = heading_matched
        return {
            "case_id": case["case_id"],
            "retrieved": rank is not None,
            "matched_rank": rank,
            "citation_ok": bool(rank is not None and heading_matched),
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
        refusals = [item for item in results if item["refusal_ok"] is not None]
        correct_refusals = sum(1 for item in refusals if item["refusal_ok"])
        passed = sum(
            1 for item in results
            if (item["refusal_ok"] if item["refusal_ok"] is not None else item["citation_ok"])
        )
        return {
            "total_cases": len(results),
            "passed_cases": passed,
            "failed_cases": len(results) - passed,
            "recall_at_k": _ratio(retrieved, len(expected)),
            "citation_accuracy": _ratio(cited, len(expected)),
            "refusal_accuracy": _ratio(correct_refusals, len(refusals)),
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
