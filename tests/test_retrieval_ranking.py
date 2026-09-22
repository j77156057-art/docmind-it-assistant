"""Retrieval-ranking fixes for citation accuracy (embedding-ab §4).

Covers the two ranking changes in ``HybridRetriever.retrieve``:
  * same-(source_key, heading) de-duplication within the ranked top-k, and
  * title-block down-weighting with a recall guard (a doc whose only candidate
    chunk is a title block must still be returned).
And the chunker's ``is_title_block`` heuristic.
"""
import tempfile
import unittest
from pathlib import Path

from ingestion.chunker import chunk_document
from ingestion.parsers import ParsedDocument, ParsedSection

from backend import HybridRetriever
from backend.embeddings import EmbeddingClient


def _chunk_candidates(*entries: dict) -> list[dict]:
    """Build candidate dicts shaped like those returned by ``hybrid_search``."""
    return [dict(entry) for entry in entries]


class StubDatabase:
    """Returns a fixed candidate list regardless of the query (no real search)."""

    def __init__(self, candidates: list[dict]):
        self.candidates = candidates

    def has_indexed_chunks(self) -> bool:
        return True

    def record_embedding_usages(self, *, usages, request_id, query_id, document_version_id):
        return None

    def hybrid_search(self, query, vector, limit, *, subject_id, roles,
                      allow_confidential=False) -> list[dict]:
        return list(self.candidates)

    def lexical_search(self, query, limit, *, subject_id, roles,
                       allow_confidential=False) -> list[dict]:
        return list(self.candidates)


def _hash_embeddings() -> EmbeddingClient:
    return EmbeddingClient(
        mode="hash", provider="builtin", model="hash-1024",
        base_url="", api_key="",
    )


class ChunkerTitleBlockTests(unittest.TestCase):
    def test_title_only_section_flagged(self):
        doc = ParsedDocument(
            title="手册", mime_type="text/markdown",
            sections=[ParsedSection("网络故障排查", "网络故障排查", 1)],
        )
        chunks = chunk_document(doc, max_chars=300, overlap_chars=40)
        self.assertTrue(chunks)
        self.assertTrue(chunks[0].is_title_block)

    def test_normal_section_not_flagged(self):
        doc = ParsedDocument(
            title="手册", mime_type="text/markdown",
            sections=[ParsedSection(
                "网络故障排查",
                "网络故障排查时，请先重启路由器并检查网线是否松动。",
                1,
            )],
        )
        chunks = chunk_document(doc, max_chars=300, overlap_chars=40)
        self.assertTrue(chunks)
        self.assertFalse(chunks[0].is_title_block)


class RetrievalRankingTests(unittest.TestCase):
    def test_same_heading_duplicate_is_dropped(self):
        candidates = _chunk_candidates(
            {"source_key": "handbook/vpn", "heading": "VPN 凭据过期", "ordinal": 1,
             "content": "请更新密码", "is_title_block": False, "score": 0.9},
            {"source_key": "handbook/vpn", "heading": "VPN 凭据过期", "ordinal": 5,
             "content": "仅标题", "is_title_block": True, "score": 0.8},
        )
        retriever = HybridRetriever(StubDatabase(candidates), _hash_embeddings(), top_k=5)
        result = retriever.retrieve("VPN 凭据过期怎么处理", subject_id="session-doc")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ordinal"], 1)

    def test_title_only_doc_survives_recall_guard(self):
        candidates = _chunk_candidates(
            {"source_key": "handbook/title-only", "heading": "目录", "ordinal": 1,
             "content": "目录", "is_title_block": True, "score": 0.7},
            {"source_key": "handbook/vpn", "heading": "VPN 凭据过期", "ordinal": 2,
             "content": "请更新密码后重连 VPN", "is_title_block": False, "score": 0.9},
        )
        retriever = HybridRetriever(StubDatabase(candidates), _hash_embeddings(), top_k=5)
        result = retriever.retrieve("VPN 凭据", subject_id="session-doc")
        sources = {hit["source_key"] for hit in result}
        self.assertIn("handbook/title-only", sources)
        self.assertIn("handbook/vpn", sources)

    def test_substantive_leads_title_block_trails(self):
        # Different headings so de-duplication does not merge them; this isolates the
        # title-block down-weight: the title block (higher raw score) must trail the
        # substantive chunk of the same source.
        candidates = _chunk_candidates(
            {"source_key": "handbook/a", "heading": "目录", "ordinal": 1,
             "content": "仅标题", "is_title_block": True, "score": 0.95},
            {"source_key": "handbook/a", "heading": "安装步骤", "ordinal": 2,
             "content": "下载安装包并运行向导完成安装", "is_title_block": False, "score": 0.9},
        )
        retriever = HybridRetriever(StubDatabase(candidates), _hash_embeddings(), top_k=5)
        result = retriever.retrieve("安装步骤", subject_id="session-doc")
        self.assertEqual(result[0]["ordinal"], 2)
        self.assertEqual(result[1]["ordinal"], 1)


if __name__ == "__main__":
    unittest.main()
