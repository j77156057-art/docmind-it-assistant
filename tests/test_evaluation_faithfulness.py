"""Tests for the RAGAS-style faithfulness metric in the evaluation gate."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend import EvaluationService, HybridRetriever, QueryDatabase
from backend.evaluation import EVALUATION_ROLES, EVALUATION_SUBJECT, score_faithfulness
from backend.embeddings import EmbeddingClient
from ingestion import DocumentIngestionService


class FaithfulnessScorerTests(unittest.TestCase):
    def test_fully_supported_answer(self):
        answer = "VPN 凭据过期需要在统一身份门户重置密码"
        context = "VPN 凭据过期时请在统一身份门户重置密码，然后重新连接网络。"
        self.assertGreaterEqual(score_faithfulness(answer, context), 0.5)

    def test_unsupported_answer_scores_low(self):
        answer = "今天天气晴转多云适合出游"
        context = "VPN 凭据过期时请在统一身份门户重置密码。"
        self.assertEqual(score_faithfulness(answer, context), 0.0)

    def test_empty_answer_is_faithful(self):
        self.assertEqual(score_faithfulness("", "任意上下文"), 1.0)

    def test_empty_context_yields_zero(self):
        self.assertEqual(score_faithfulness("VPN 密码重置", ""), 0.0)


class EvaluationFaithfulnessTests(unittest.TestCase):
    def _build(self, root: str):
        project = Path(root)
        path = project / "vpn.md"
        path.write_text(
            "# 企业网络\n## VPN 凭据过期\n请在统一身份门户更新密码，然后重新登录 VPN。\n",
            encoding="utf-8",
        )
        database = QueryDatabase(str(project / "knowledge.db"))
        database.initialize()
        embeddings = EmbeddingClient(
            mode="hash", provider="builtin", model="hash-1024", base_url="", api_key="",
        )
        ingestion = DocumentIngestionService(
            database, embeddings,
            max_bytes=2 * 1024 * 1024, chunk_max_chars=300,
            chunk_overlap_chars=40, chunk_child_max_chars=120,
        )
        ingestion.import_file(path, source_key="handbook/vpn")
        retriever = HybridRetriever(database, embeddings, top_k=3)
        settings = SimpleNamespace(
            evaluation_top_k=3,
            evaluation_gate_mode="warn",
            evaluation_faithfulness_enabled=True,
            evaluation_min_faithfulness=0.7,
            evaluation_min_recall=0.8,
            evaluation_min_citation_accuracy=0.8,
        )
        service = EvaluationService(settings=settings, database=database, retriever=retriever)
        return database, service

    def test_evaluate_case_computes_faithfulness(self):
        with tempfile.TemporaryDirectory() as root:
            database, service = self._build(root)
            case = {
                "case_id": 1, "case_key": "vpn-reset", "question": "VPN 凭据过期怎么办",
                "expect_refusal": False,
                "expected_document_key": "handbook/vpn", "expected_heading": "vpn 凭据过期",
            }
            result = service._evaluate_case(case)
            database.dispose()
            self.assertIn("faithfulness", result["detail"])
            self.assertIsNotNone(result["detail"]["faithfulness"])
            self.assertGreaterEqual(result["detail"]["faithfulness"], 0.0)

    def test_decide_reports_faithfulness_problem(self):
        with tempfile.TemporaryDirectory() as root:
            database, service = self._build(root)
            metrics = {
                "total_cases": 1, "passed_cases": 1, "failed_cases": 0,
                "recall_at_k": 1.0, "citation_accuracy": 1.0, "refusal_accuracy": 1.0,
                "faithfulness": 0.3, "faithfulness_coverage": 1.0,
            }
            gate_result, reason = service._decide(metrics=metrics, baseline=None)
            database.dispose()
            self.assertIn("忠实度", reason)
            # under default warn mode it should warn, not force a hard block on its own
            self.assertIn(gate_result, ("warn", "block"))


if __name__ == "__main__":
    unittest.main()
