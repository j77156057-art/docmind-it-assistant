"""Asynchronous indexing worker: claims queued jobs and drives the ingestion pipeline.

The business queue (``ingestion_jobs``) is owned by this application, not by an orchestration
framework: administrators can see, retry and cancel jobs, and the audit trail does not depend on
a framework's internal checkpoint format.
"""
from __future__ import annotations

import logging
import os
import socket
import time

from backend import DocumentSourceStore, log_event, request_id_context
from ingestion import DocumentProcessingError

from .graph import StagingUnavailable

LOGGER = logging.getLogger("docmind.it.worker")


def default_worker_id() -> str:
    return f"{socket.gethostname()[:32]}-{os.getpid()}"


class IngestionWorker:
    """Single-process worker loop.

    Horizontal scaling is the database's job: every worker claims with an atomic conditional
    update, so PostgreSQL's ``FOR UPDATE SKIP LOCKED`` keeps replicas from double-processing.
    """

    def __init__(self, *, settings, database, ingestion, sources: DocumentSourceStore,
                 worker_id: str = "", engine: str = "", graph_runner=None):
        self.settings = settings
        self.database = database
        self.ingestion = ingestion
        self.sources = sources
        self.worker_id = (
            worker_id or settings.ingestion_worker_id or default_worker_id()
        )[:64]
        self.engine = (engine or settings.ingestion_engine or "simple").strip().lower()
        self._graph_runner = graph_runner

    def _runner(self):
        """Lazily build the graph runner so the core deployment never imports the framework."""
        if self._graph_runner is None:
            from .graph import IndexingGraphRunner  # noqa: PLC0415 - deliberate lazy import

            self._graph_runner = IndexingGraphRunner(
                settings=self.settings, database=self.database, ingestion=self.ingestion,
            )
        return self._graph_runner

    def close(self) -> None:
        if self._graph_runner is not None:
            self._graph_runner.close()

    @property
    def publish_on_success(self) -> bool:
        """Review mode stops at ``staged``; direct mode publishes as soon as indexing finishes."""
        return self.settings.governance_mode != "review"

    def run_once(self) -> dict | None:
        """Reclaim abandoned jobs, claim at most one job, and execute it."""
        reclaimed = self.database.reclaim_stale_ingestion_jobs(
            timeout_seconds=self.settings.ingestion_job_timeout_seconds,
        )
        if reclaimed:
            log_event(LOGGER, logging.WARNING, "ingestion_jobs_reclaimed", count=reclaimed)
        job = self.database.claim_ingestion_job(self.worker_id)
        if job is None:
            return None
        started = time.perf_counter()
        outcome = self._execute(job)
        log_event(
            LOGGER, logging.INFO, "ingestion_job_finished",
            job_id=job["job_id"], job_type=job["job_type"], status=outcome["status"],
            error_code=outcome.get("error_code"), attempts=outcome.get("attempts"),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return outcome

    def drain(self, *, limit: int = 0) -> int:
        """Process available jobs until the queue is empty (used by ``--once`` and tests)."""
        processed = 0
        while True:
            outcome = self.run_once()
            if outcome is None:
                break
            processed += 1
            if limit and processed >= int(limit):
                break
        return processed

    def run_forever(self, *, poll_seconds: float | None = None, max_jobs: int = 0,
                    sleep=time.sleep) -> int:
        interval = max(
            0.2,
            float(poll_seconds if poll_seconds is not None else self.settings.ingestion_poll_seconds),
        )
        processed = 0
        retry_backoff = 0.0
        try:
            while True:
                outcome = self.run_once()
                if outcome is None:
                    retry_backoff = 0.0
                    sleep(interval)
                    continue
                processed += 1
                if max_jobs and processed >= int(max_jobs):
                    return processed
                # A retryable failure normally goes straight back to ``queued``. Back off in-process
                # so a broken provider cannot be hammered in a tight loop. A durable
                # ``next_attempt_at`` column is deliberately left to a later batch.
                if outcome["status"] == "queued" and outcome.get("retryable"):
                    retry_backoff = min(30.0, max(interval, retry_backoff * 2 or interval))
                    sleep(retry_backoff)
        except KeyboardInterrupt:
            log_event(LOGGER, logging.INFO, "ingestion_worker_interrupted", processed=processed)
            return processed

    def _execute(self, job: dict) -> dict:
        job_id = job["job_id"]
        version_id = job.get("version_id")
        request_id = job.get("request_id") or f"job-{job_id}"
        token = request_id_context.set(request_id)
        try:
            if job["job_type"] != "import":
                # Explicitly fail instead of pretending to succeed: reindex/withdraw/evaluate
                # arrive in later batches.
                raise DocumentProcessingError("job_type_unsupported", retryable=False)
            if not version_id or not job.get("document_id"):
                raise DocumentProcessingError("job_target_missing", retryable=False)
            reference = self.database.document_version_reference(version_id)
            source_path = self.sources.resolve(reference["document_id"], reference["version"])
            if source_path is None:
                raise DocumentProcessingError("source_missing", retryable=False)
            self.database.heartbeat_ingestion_job(job_id, self.worker_id)
            if self.engine == "langgraph":
                # Checkpointed orchestration: a resumed run skips batches it already embedded.
                result = self._runner().run(
                    job=job,
                    document_id=reference["document_id"],
                    version_id=version_id,
                    source_path=source_path,
                )
            else:
                result = self.ingestion.process_version(
                    document_id=reference["document_id"],
                    version_id=version_id,
                    source_path=source_path,
                    publish=self.publish_on_success,
                    actor_subject_id=job.get("created_by_subject_id") or "",
                    request_id=request_id,
                )
        except DocumentProcessingError as exc:
            return self._record_failure(job, exc.code, retryable=exc.retryable, detail=exc.detail)
        except StagingUnavailable as exc:
            # The checkpoint survived but its staged payload did not; restarting is deterministic.
            return self._record_failure(
                job, "staging_missing", retryable=True, detail=type(exc).__name__,
            )
        except ValueError as exc:
            return self._record_failure(
                job, "job_target_missing", retryable=False, detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - worker boundary must not kill the loop
            return self._record_failure(
                job, "worker_error", retryable=True, detail=type(exc).__name__,
            )
        self.database.heartbeat_ingestion_job(job_id, self.worker_id)
        self.database.complete_ingestion_job(job_id, self.worker_id)
        self.database.record_audit_event(
            actor_subject_id=(job.get("created_by_subject_id") or "system:worker")[:64],
            action="document_index_completed",
            target_type="document",
            target_ref=f"{result['document_id']}:v{reference['version']}",
            result="success",
            request_id=request_id,
        )
        log_event(
            LOGGER, logging.INFO, "ingestion_job_indexed",
            job_id=job_id, status=result["status"], chunk_count=result["chunk_count"],
        )
        return {
            "job_id": job_id,
            "status": "succeeded",
            "job_type": job["job_type"],
            "index_status": result["status"],
            "chunk_count": result["chunk_count"],
        }

    def _record_failure(self, job: dict, code: str, *, retryable: bool,
                        detail: str = "") -> dict:
        outcome = self.database.fail_ingestion_job(job["job_id"], code, retryable=retryable)
        version_id = job.get("version_id")
        if version_id:
            if outcome["status"] == "queued":
                self.database.reset_document_version_for_retry(version_id)
            else:
                self.database.fail_document_import(version_id, code)
        terminal = outcome["status"] == "failed"
        self.database.record_audit_event(
            actor_subject_id=(job.get("created_by_subject_id") or "system:worker")[:64],
            action="document_index_failed",
            target_type="document",
            target_ref=f"{job.get('document_id')}:{job.get('version_id')}",
            result="failed",
            request_id=job.get("request_id") or f"job-{job['job_id']}",
        )
        log_event(
            LOGGER, logging.WARNING, "ingestion_job_failed",
            job_id=job["job_id"], error_code=code, retryable=retryable, terminal=terminal,
            # Short diagnostic token (an exception type name or an error code); free-form detail
            # must not reach the log allowlist.
            error_type=(detail or code)[:64],
        )
        return {
            "job_id": job["job_id"],
            "status": outcome["status"],
            "error_code": code,
            "attempts": outcome["attempts"],
            "retryable": retryable,
        }
