"""LangGraph orchestration for a single indexing job (opt-in worker engine).

Why a graph at all
------------------
The expensive, non-repeatable step of indexing is embedding: it costs money and time per batch.
A plain function that parses, embeds and commits in one go has to start over after a crash, so a
worker killed at batch 7 of 10 pays for batches 1-6 twice. The graph checkpoints after every
batch, so a resumed run continues where it stopped.

Design constraints (see docs/isolation-boundary.md):

* **This module is worker-only.** Nothing under ``app.py``, ``assistant/`` or ``backend/`` may
  import it, directly or transitively. ``tests/test_isolation_boundary.py`` proves that by
  walking the import closure from the query entry point.
* **Business state stays ours.** ``ingestion_jobs`` and ``document_versions`` remain the source of
  truth for administrators and auditors; checkpoint tables hold framework-internal resume state
  and are never read by the query or admin services.
* **Checkpoints never share the application schema.** Writing them into the application database
  would make ``alembic check`` report unknown tables, so they live in a separate SQLite file
  (default) or a separate PostgreSQL schema.
* **State stays small.** Chunk payloads are staged on disk, not in graph state, so checkpoints do
  not grow with document size.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, TypedDict

from backend import log_event
from ingestion import DocumentProcessingError

LOGGER = logging.getLogger("docmind.it.worker.graph")

CHECKPOINT_SCHEMA = "langgraph_checkpoint"


class StagingUnavailable(RuntimeError):
    """The staged chunk file is gone, so a checkpointed run cannot be resumed."""


class IndexingState(TypedDict, total=False):
    job_id: int
    document_id: int
    version_id: int
    source_path: str
    staging_path: str
    publish: bool
    actor_subject_id: str
    request_id: str
    batch_size: int
    batch_total: int
    cursor: int
    chunk_count: int
    status: str
    title: str


def langgraph_available() -> bool:
    try:
        import langgraph.graph  # noqa: F401
    except ImportError:
        return False
    return True


def checkpoint_dsn(database_url: str) -> str:
    """PostgreSQL DSN for the checkpointer: psycopg's own scheme, not SQLAlchemy's."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def build_indexing_graph(*, service, database, checkpointer):
    """Compile the pipeline. Kept separate from the runner so tests can inject a checkpointer."""
    from langgraph.graph import END, START, StateGraph

    def prepare(state: IndexingState) -> dict:
        version_id = int(state["version_id"])
        parsed, chunks = service.parse_and_chunk(state["source_path"])
        database.mark_document_version_processing(
            version_id, title=parsed.title, mime_type=parsed.mime_type,
        )
        # A fresh run must not inherit rows from a previous attempt; batches are keyed by ordinal.
        database.clear_document_version_chunks(version_id)
        staging = Path(state["staging_path"])
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_text(
            json.dumps([
                {
                    "ordinal": chunk.ordinal,
                    "heading": chunk.heading,
                    "page_number": chunk.page_number,
                    "content": chunk.content,
                }
                for chunk in chunks
            ], ensure_ascii=False),
            encoding="utf-8",
        )
        batch_size = max(1, int(state.get("batch_size") or 1))
        return {
            "batch_size": batch_size,
            "batch_total": (len(chunks) + batch_size - 1) // batch_size,
            "cursor": 0,
            "chunk_count": len(chunks),
            "title": parsed.title,
        }

    def embed_batch(state: IndexingState) -> dict:
        staging = Path(state["staging_path"])
        if not staging.is_file():
            raise StagingUnavailable(str(staging))
        payload = json.loads(staging.read_text(encoding="utf-8"))
        cursor = int(state.get("cursor") or 0)
        size = max(1, int(state.get("batch_size") or 1))
        batch = payload[cursor * size:(cursor + 1) * size]
        if not batch:
            return {"cursor": cursor}
        result = service.embed_texts(
            [f"{row['heading']}\n{row['content']}".strip() for row in batch],
            request_id=state["request_id"], document_version_id=int(state["version_id"]),
        )
        database.replace_document_chunk_batch(
            int(state["version_id"]),
            [dict(row, embedding=list(vector)) for row, vector in zip(batch, result.vectors)],
        )
        return {"cursor": cursor + 1}

    def finalize(state: IndexingState) -> dict:
        version = database.finalize_document_version(
            int(state["version_id"]),
            chunk_count=int(state.get("chunk_count") or 0),
            publish=bool(state.get("publish")),
            actor_subject_id=state.get("actor_subject_id") or "",
            request_id=state.get("request_id") or "",
        )
        staging = state.get("staging_path")
        if staging:
            Path(staging).unlink(missing_ok=True)
        return {"status": version["status"]}

    def route(state: IndexingState) -> str:
        return (
            "finalize"
            if int(state.get("cursor") or 0) >= int(state.get("batch_total") or 0)
            else "embed_batch"
        )

    graph = StateGraph(IndexingState)
    graph.add_node("prepare", prepare)
    graph.add_node("embed_batch", embed_batch)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "embed_batch")
    graph.add_conditional_edges(
        "embed_batch", route, {"embed_batch": "embed_batch", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


class IndexingGraphRunner:
    """Owns the compiled graph and its checkpointer for the lifetime of the worker."""

    def __init__(self, *, settings, database, ingestion, checkpointer_factory=None):
        self.settings = settings
        self.database = database
        self.ingestion = ingestion
        self._factory = checkpointer_factory
        self._graph: Any = None
        self._close_callback = None

    # -- checkpointer plumbing -------------------------------------------------
    def _open_checkpointer(self):
        if self.database.backend == "postgresql":
            return self._open_postgres_checkpointer()
        return self._open_sqlite_checkpointer()

    def _open_sqlite_checkpointer(self):
        from langgraph.checkpoint.sqlite import SqliteSaver

        path = Path(self.settings.ingestion_checkpoint_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, check_same_thread=False)
        saver = SqliteSaver(connection)
        saver.setup()

        def closer() -> None:
            connection.close()

        return saver, closer

    def _open_postgres_checkpointer(self):
        """PostgreSQL checkpoints live in their own schema so alembic never sees them."""
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg import Connection
        from psycopg.rows import dict_row

        connection = Connection.connect(
            checkpoint_dsn(self.settings.database_url), autocommit=True, row_factory=dict_row,
        )
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{CHECKPOINT_SCHEMA}"')
            cursor.execute(f'SET search_path TO "{CHECKPOINT_SCHEMA}", public')
        saver = PostgresSaver(connection)
        saver.setup()

        def closer() -> None:
            connection.close()

        return saver, closer

    def graph(self):
        if self._graph is None:
            if not langgraph_available():
                raise DocumentProcessingError(
                    "langgraph_not_installed",
                    detail="pip install -r requirements-worker.txt",
                    retryable=False,
                )
            if self._factory is not None:
                saver, closer = self._factory()
            else:
                saver, closer = self._open_checkpointer()
            self._graph = build_indexing_graph(
                service=self.ingestion, database=self.database, checkpointer=saver,
            )
            self._close_callback = closer
        return self._graph

    def close(self) -> None:
        if self._close_callback is not None:
            try:
                self._close_callback()
            finally:
                self._close_callback = None
                self._graph = None

    # -- execution -------------------------------------------------------------
    def _thread(self, job_id: int) -> dict:
        return {"configurable": {"thread_id": f"index-job-{int(job_id)}"}}

    def _has_checkpoint(self, graph, config: dict) -> bool:
        checkpointer = getattr(graph, "checkpointer", None)
        getter = getattr(checkpointer, "get_tuple", None)
        if getter is None:
            return False
        try:
            return getter(config) is not None
        except Exception:  # noqa: BLE001 - a missing checkpoint is not an error
            return False

    def run(self, *, job: dict, document_id: int, version_id: int, source_path,
            staging_path=None) -> dict:
        """Run or resume one indexing job.

        A checkpoint is only resumed when its staged chunk file still exists; otherwise the thread
        is discarded and the pipeline restarts from parsing.
        """
        graph = self.graph()
        config = self._thread(job["job_id"])
        staging = Path(staging_path) if staging_path else (
            Path(source_path).parent / f"v{version_id}.chunks.json"
        )
        has_checkpoint = self._has_checkpoint(graph, config)
        resuming = has_checkpoint and staging.is_file()
        if has_checkpoint and not resuming:
            # Staged payload is gone: drop the stale checkpoint and start over.
            checkpointer = getattr(graph, "checkpointer", None)
            deleter = getattr(checkpointer, "delete_thread", None)
            if deleter is not None:
                deleter(config["configurable"]["thread_id"])
            log_event(LOGGER, logging.WARNING, "indexing_checkpoint_discarded",
                      job_id=job["job_id"], version_id=version_id)
        if resuming:
            # The version may have been reset to `queued` by a retry; re-claim it for processing
            # without re-parsing the document.
            self.database.mark_document_version_processing(version_id)
            log_event(LOGGER, logging.INFO, "indexing_resumed_from_checkpoint",
                      job_id=job["job_id"], version_id=version_id)
            result = graph.invoke(None, config)
        else:
            result = graph.invoke({
                "job_id": int(job["job_id"]),
                "document_id": int(document_id),
                "version_id": int(version_id),
                "source_path": str(source_path),
                "staging_path": str(staging),
                "publish": self.settings.governance_mode != "review",
                "actor_subject_id": job.get("created_by_subject_id") or "",
                "request_id": job.get("request_id") or f"job-{job['job_id']}",
                "batch_size": max(1, int(self.ingestion.embeddings.batch_size)),
            }, config)
        return {
            "document_id": int(document_id),
            "version_id": int(version_id),
            "status": result.get("status") or "staged",
            "title": result.get("title") or "",
            "chunk_count": int(result.get("chunk_count") or 0),
            "embedding_mode": self.ingestion.embeddings.mode,
            "resumed": resuming,
        }
