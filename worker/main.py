"""Entry point for the asynchronous indexing worker.

Run with ``.venv\\Scripts\\python -m worker`` (or ``-m worker --once`` to drain the queue and
exit). The worker shares configuration with the admin service so the two always agree on
governance mode, embedding provider and retry policy.
"""
from __future__ import annotations

import argparse
import logging

from backend import (
    AppSettings, DocumentSourceStore, QueryDatabase, build_embedding_client, configure_logging,
    log_event,
)
from ingestion import DocumentIngestionService

from .runner import IngestionWorker

LOGGER = logging.getLogger("docmind.it.worker")


def build_worker(settings: AppSettings) -> tuple[IngestionWorker, QueryDatabase]:
    database = QueryDatabase(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout,
        connect_timeout=settings.database_connect_timeout,
        query_field_key=settings.query_field_key.get_secret_value(),
        retention_days=settings.retention_days,
        retention_grace_days=settings.retention_grace_days,
    )
    ingestion = DocumentIngestionService(
        database,
        build_embedding_client(settings),
        max_bytes=settings.document_max_bytes,
        chunk_max_chars=settings.chunk_max_chars,
        chunk_overlap_chars=settings.chunk_overlap_chars,
        max_characters=settings.document_max_characters,
        max_pages=settings.document_max_pages,
        require_review=settings.governance_mode == "review",
    )
    sources = DocumentSourceStore(settings.project_root / "data" / "sources")
    worker = IngestionWorker(
        settings=settings, database=database, ingestion=ingestion, sources=sources,
    )
    return worker, database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docmind-worker", description="DocMind 异步索引 Worker")
    parser.add_argument("--once", action="store_true", help="处理完当前队列后退出")
    parser.add_argument("--max-jobs", type=int, default=0, help="最多处理多少个任务（0 表示不限）")
    parser.add_argument("--poll-seconds", type=float, default=None, help="空闲轮询间隔秒数")
    parser.add_argument("--worker-id", default="", help="Worker 标识，默认取主机名与进程号")
    parser.add_argument(
        "--engine", choices=("simple", "langgraph"), default="",
        help="索引编排引擎，默认取 IT_INGESTION_ENGINE",
    )
    args = parser.parse_args(argv)

    settings = AppSettings.from_environment()
    configure_logging(settings)
    worker, database = build_worker(settings)
    if args.worker_id:
        worker.worker_id = args.worker_id[:64]
    if args.engine:
        worker.engine = args.engine
    try:
        database.initialize()
        worker.sources.initialize()
        log_event(
            LOGGER, logging.INFO, "ingestion_worker_started",
            worker_id=worker.worker_id,
            engine=worker.engine,
            publish_on_success=worker.publish_on_success,
            poll_seconds=args.poll_seconds or settings.ingestion_poll_seconds,
            max_attempts=settings.ingestion_max_attempts,
        )
        if args.once:
            processed = worker.drain(limit=args.max_jobs)
        else:
            processed = worker.run_forever(
                poll_seconds=args.poll_seconds, max_jobs=args.max_jobs,
            )
        log_event(LOGGER, logging.INFO, "ingestion_worker_stopped", processed=processed)
        return 0
    finally:
        worker.close()
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
