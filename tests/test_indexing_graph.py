"""LangGraph orchestration tests: checkpointed resume and checkpoint isolation.

The property that matters commercially is the resume test: after a worker dies mid-pipeline, a
resumed run must not pay for the embedding batches that already succeeded.
"""
from pathlib import Path
import tempfile
import unittest

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from admin_app import create_admin_app
from backend import AppSettings, QueryDatabase
from backend.embeddings import EmbeddingClient, EmbeddingError
from ingestion import DocumentIngestionService
from ingestion.chunker import chunk_document
from ingestion.parsers import parse_document
from worker.graph import build_indexing_graph, checkpoint_dsn, langgraph_available
from worker.main import build_worker
from worker.runner import IngestionWorker


ROOT = Path(__file__).resolve().parents[1]
CONTENT = (
    "# 打印机\n## 驱动安装\nzebra printer driver 需要重新安装驱动。\n"
    "## 纸张设置\n请选择 A4 纸张并重新校准。\n"
    "## 网络打印\n请检查打印服务器地址与端口。\n"
    "## 常见故障\n卡纸时请先断电再取出纸张。\n"
)


class CountingEmbeddingClient(EmbeddingClient):
    """Hash embeddings with a call counter; no network is involved."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = 0

    def embed(self, texts, *, request_id: str = ""):
        self.calls += 1
        return super().embed(texts, request_id=request_id)


class FlakyEmbeddingClient(CountingEmbeddingClient):
    """Fails deterministically after a number of successful batches."""

    def __init__(self, *, succeed_first: int, **kwargs):
        super().__init__(**kwargs)
        self.succeed_first = succeed_first

    def embed(self, texts, *, request_id: str = ""):
        if self.calls >= self.succeed_first:
            self.calls += 1
            raise EmbeddingError("embedding_unavailable")
        return super().embed(texts, request_id=request_id)


def migration_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


class IndexingGraphTests(unittest.TestCase):
    def make_settings(self, root: str, *, engine: str = "langgraph") -> AppSettings:
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
            governance_mode="direct",
            ingestion_worker_enabled=True,
            ingestion_engine=engine,
            ingestion_checkpoint_path=project / "data" / "worker-checkpoints.db",
            # Retry backoff is exercised in test_ingestion_backoff; checkpoint-resume tests need
            # the requeued job to be claimable immediately, so disable the delay here.
            ingestion_backoff_max_seconds=0,
        )

    @staticmethod
    def headers(subject: str, roles: str) -> dict[str, str]:
        return {"X-Auth-Subject": subject, "X-Auth-Roles": roles}

    def expected_chunk_count(self, settings: AppSettings, path: Path) -> int:
        parsed = parse_document(
            path, title="", max_bytes=settings.document_max_bytes,
            max_characters=settings.document_max_characters, max_pages=settings.document_max_pages,
        )
        return len(chunk_document(
            parsed, max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars,
        ))

    def worker_with_client(self, settings: AppSettings, client: EmbeddingClient):
        database = QueryDatabase(settings.database_url)
        ingestion = DocumentIngestionService(
            database, client,
            max_bytes=settings.document_max_bytes,
            chunk_max_chars=settings.chunk_max_chars,
            chunk_overlap_chars=settings.chunk_overlap_chars,
            max_characters=settings.document_max_characters,
            max_pages=settings.document_max_pages,
            require_review=settings.governance_mode == "review",
        )
        from backend import DocumentSourceStore

        worker = IngestionWorker(
            settings=settings, database=database, ingestion=ingestion,
            sources=DocumentSourceStore(settings.project_root / "data" / "sources"),
            worker_id="graph-test", engine="langgraph",
        )
        return worker, database

    def import_document(self, client: TestClient, settings: AppSettings,
                        *, content: str = CONTENT, filename: str = "printer.md",
                        source_key: str = "manual/printer") -> dict:
        response = client.post(
            "/api/admin/documents/import",
            headers=self.headers("editor-1", "knowledge_editor"),
            files={"file": (filename, content.encode("utf-8"), "text/markdown")},
            data={"source_key": source_key, "access_scope": "public",
                  "classification": "internal"},
        )
        self.assertEqual(response.status_code, 202)
        return response.json()

    @unittest.skipUnless(langgraph_available(), "worker extras are not installed")
    def test_checkpoint_resume_does_not_re_embed_finished_batches(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            database = QueryDatabase(settings.database_url)
            try:
                with TestClient(application) as client:
                    imported = self.import_document(client, settings)
                    version_id = imported["version_id"]
                    document_id = imported["document_id"]
                    source = (
                        settings.project_root / "data" / "sources" / str(document_id)
                    )
                    source_file = next(source.glob("v1-*"))
                total_chunks = self.expected_chunk_count(settings, source_file)

                # First attempt: succeeds for two batches, then the provider goes away.
                flaky = FlakyEmbeddingClient(
                    succeed_first=2, mode="hash", provider="builtin", model="hash-1024",
                    base_url="", api_key="", dimension=1024, batch_size=1,
                )
                first_worker, first_database = self.worker_with_client(settings, flaky)
                try:
                    # A single attempt: `drain()` would immediately retry the requeued job.
                    outcome = first_worker.run_once()
                finally:
                    first_worker.close()
                    first_database.dispose()
                self.assertEqual(outcome["status"], "queued")
                self.assertTrue(outcome["retryable"])
                self.assertGreaterEqual(flaky.calls, 3)

                job = [item for item in database.list_ingestion_jobs()
                       if item["version_id"] == version_id][0]
                self.assertEqual(job["status"], "queued")
                self.assertEqual(job["last_error_code"], "embedding_unavailable")
                self.assertEqual(database.count_document_chunks(version_id), 2)
                self.assertTrue(list(source.glob("*.chunks.json")))

                # Second attempt resumes from the checkpoint: only the remaining batches run.
                healthy = CountingEmbeddingClient(
                    mode="hash", provider="builtin", model="hash-1024",
                    base_url="", api_key="", dimension=1024, batch_size=1,
                )
                second_worker, second_database = self.worker_with_client(settings, healthy)
                try:
                    self.assertEqual(second_worker.run_once()["status"], "succeeded")
                finally:
                    second_worker.close()
                    second_database.dispose()

                self.assertEqual(healthy.calls, total_chunks - 2)
                self.assertEqual(database.count_document_chunks(version_id), total_chunks)
                completed = [item for item in database.list_ingestion_jobs()
                             if item["version_id"] == version_id][0]
                self.assertEqual(completed["status"], "succeeded")
                self.assertEqual(
                    [row["status"] for row in database.list_documents()], ["indexed"],
                )
                self.assertTrue(
                    database.lexical_search("zebra", subject_id="v1", roles=("viewer",)),
                )
            finally:
                database.dispose()

    @unittest.skipUnless(langgraph_available(), "worker extras are not installed")
    def test_missing_staging_discards_the_checkpoint_and_restarts(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(root)
            application = create_admin_app(settings)
            database = QueryDatabase(settings.database_url)
            try:
                with TestClient(application) as client:
                    imported = self.import_document(client, settings)
                    document_id = imported["document_id"]
                    source = settings.project_root / "data" / "sources" / str(document_id)
                    source_file = next(source.glob("v1-*"))
                total_chunks = self.expected_chunk_count(settings, source_file)

                flaky = FlakyEmbeddingClient(
                    succeed_first=1, mode="hash", provider="builtin", model="hash-1024",
                    base_url="", api_key="", dimension=1024, batch_size=1,
                )
                first_worker, first_database = self.worker_with_client(settings, flaky)
                try:
                    self.assertEqual(first_worker.run_once()["status"], "queued")
                finally:
                    first_worker.close()
                    first_database.dispose()

                # Remove the staged payload: the checkpoint is now unusable and must be dropped.
                for staged in source.glob("*.chunks.json"):
                    staged.unlink()

                healthy = CountingEmbeddingClient(
                    mode="hash", provider="builtin", model="hash-1024",
                    base_url="", api_key="", dimension=1024, batch_size=1,
                )
                second_worker, second_database = self.worker_with_client(settings, healthy)
                try:
                    self.assertEqual(second_worker.run_once()["status"], "succeeded")
                finally:
                    second_worker.close()
                    second_database.dispose()

                self.assertEqual(healthy.calls, total_chunks)
                self.assertEqual(database.count_document_chunks(imported["version_id"]),
                                 total_chunks)
            finally:
                database.dispose()

    @unittest.skipUnless(langgraph_available(), "worker extras are not installed")
    def test_checkpoints_never_pollute_the_application_schema(self):
        with tempfile.TemporaryDirectory() as root:
            url = f"sqlite:///{(Path(root) / 'app.db').as_posix()}"
            alembic_config = migration_config(url)
            command.upgrade(alembic_config, "head")

            settings = self.make_settings(root).model_copy(update={"database_url": url})
            application = create_admin_app(settings)
            database = QueryDatabase(url)
            try:
                with TestClient(application) as client:
                    imported = self.import_document(client, settings)
                worker, worker_database = build_worker(settings)
                worker.engine = "langgraph"
                try:
                    self.assertEqual(worker.drain(), 1)
                finally:
                    worker.close()
                    worker_database.dispose()

                self.assertEqual(
                    [row["status"] for row in database.list_documents()], ["indexed"],
                )
                # A migrated application database plus a separate checkpoint file: alembic must not
                # discover unknown tables, which is why checkpoints are never stored here.
                command.check(alembic_config)
                checkpoint = Path(settings.ingestion_checkpoint_path)
                self.assertTrue(checkpoint.is_file())
                with database.engine.connect() as connection:
                    names = connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                self.assertFalse([name for (name,) in names if "checkpoint" in name.lower()])
                self.assertEqual(imported["document_id"], 1)
            finally:
                database.dispose()

    def test_checkpoint_dsn_and_graph_are_framework_scoped(self):
        self.assertEqual(
            checkpoint_dsn("postgresql+psycopg://user:secret@db:5432/docmind"),
            "postgresql://user:secret@db:5432/docmind",
        )
        self.assertEqual(checkpoint_dsn("sqlite:///x.db"), "sqlite:///x.db")
        if langgraph_available():
            from langgraph.checkpoint.memory import InMemorySaver

            graph = build_indexing_graph(
                service=None, database=None, checkpointer=InMemorySaver(),
            )
            self.assertEqual(
                set(graph.get_graph().nodes),
                {"__start__", "embed_batch", "finalize", "prepare", "__end__"},
            )


if __name__ == "__main__":
    unittest.main()
