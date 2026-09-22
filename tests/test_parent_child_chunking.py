"""Tests for parent-child chunking and parent_content round-trip."""
import tempfile
import unittest
from pathlib import Path

from ingestion.chunker import chunk_document
from ingestion.parsers import ParsedDocument, ParsedSection

from backend import HybridRetriever, QueryDatabase
from backend.embeddings import EmbeddingClient
from ingestion import DocumentIngestionService


def _doc_with_sections() -> ParsedDocument:
    long = (
        "第一段：员工入职需要填写登记表并上传身份证。"
        "第二段：IT 部门会在两个工作日内发放笔记本电脑与域账号。"
        "第三段：邮箱初始密码通过短信发送，首次登录必须修改。"
        "第四段：VPN 凭据过期时请在统一身份门户重置密码后重新连接。"
        "第五段：打印机无法连接无线网络时，请检查 IP 地址与子网配置是否一致。"
        "第六段：所有外发邮件超过十兆需要通过审批网关，避免数据泄露。"
    )
    section = ParsedSection("员工 IT 手册", long, 1)
    return ParsedDocument(title="员工 IT 手册", mime_type="text/markdown", sections=[section])


class ParentChildChunkingTests(unittest.TestCase):
    def test_single_level_when_no_child_size(self):
        chunks = chunk_document(_doc_with_sections(), max_chars=300, overlap_chars=40)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertEqual(chunk.parent_content, "")

    def test_child_carries_parent_content(self):
        chunks = chunk_document(
            _doc_with_sections(), max_chars=300, overlap_chars=40,
            child_max_chars=120, child_overlap_chars=20,
        )
        self.assertTrue(chunks)
        for chunk in chunks:
            # child content is a strict subset / not equal to the larger parent
            self.assertTrue(chunk.parent_content)
            self.assertLessEqual(len(chunk.content), len(chunk.parent_content) + 1)
            self.assertIn(chunk.content[:10], chunk.parent_content)

    def test_child_count_exceeds_parent_count(self):
        parent_only = chunk_document(_doc_with_sections(), max_chars=300, overlap_chars=40)
        with_children = chunk_document(
            _doc_with_sections(), max_chars=300, overlap_chars=40,
            child_max_chars=120, child_overlap_chars=20,
        )
        self.assertGreater(len(with_children), len(parent_only))

    def test_parent_content_round_trips_through_retrieval(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            path = project / "handbook.md"
            path.write_text(
                "# 员工 IT 手册\n"
                "## 入职\n员工入职需要填写登记表并上传身份证，IT 部门会在两个工作日内发放设备。\n"
                "## VPN\nVPN 凭据过期时请在统一身份门户重置密码，然后重新连接网络。\n",
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
            ingestion.import_file(path, source_key="handbook/it")

            retriever = HybridRetriever(database, embeddings, top_k=3)
            hits = retriever.retrieve("VPN 凭据过期怎么重置密码", None, subject_id="legacy")
            database.dispose()

            self.assertTrue(hits)
            top = hits[0]
            # parent_content must be present and richer than the retrieved child chunk
            self.assertTrue(top.get("parent_content"))
            self.assertIn(top["content"][:8], top["parent_content"])
            self.assertGreaterEqual(len(top["parent_content"]), len(top["content"]))


if __name__ == "__main__":
    unittest.main()
