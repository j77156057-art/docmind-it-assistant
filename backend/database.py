"""SQLAlchemy query repository supporting PostgreSQL and isolated SQLite tests."""
from __future__ import annotations

from pathlib import Path

from decimal import Decimal
from collections import Counter
from datetime import datetime, timedelta, timezone
import math
import base64
import hashlib

from sqlalchemy import and_, case, create_engine, func, inspect, or_, select, text, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from .db_models import (
    ACTIVE_JOB_STATUSES, IN_FLIGHT_STATUSES, JOB_STATUSES, AuditEventRecord, Base,
    DocumentAclRecord, DocumentChunkRecord, DocumentRecord, DocumentVersionRecord,
    DocumentVersionReviewRecord, EvaluationCaseRecord, EvaluationCaseResultRecord,
    EvaluationRunRecord, IngestionJobRecord, ModelUsageRecord, QueryRecord,
    RuntimeModelConfigRecord, RuntimeProviderCredentialRecord,
)
from cryptography.fernet import Fernet, InvalidToken
from .auth import (
    CLASSIFICATION_RANK, CONFIDENTIAL, INTERNAL, OPEN_CLASSIFICATIONS, is_open_classification,
    normalize_classification,
)
from .pricing import cost_cny, pricing_status
from .text_index import lexical_text, lexical_terms


def _database_url(value: str) -> str:
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    if "://" in value:
        return value
    return f"sqlite:///{Path(value).resolve().as_posix()}"


class GovernanceError(ValueError):
    """Illegal knowledge-governance action. ``code`` is stable for tests and alerting."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


class QueryDatabase:
    """Repository boundary; PostgreSQL schema ownership remains with Alembic."""

    def __init__(self, url_or_path: str, *, pool_size: int = 5, max_overflow: int = 10,
                 pool_timeout: int = 30, connect_timeout: int = 5, secret_key: str = ""):
        self.url = _database_url(str(url_or_path))
        url = make_url(self.url)
        engine_options: dict = {"pool_pre_ping": True}
        if url.get_backend_name() == "sqlite":
            database = url.database or ""
            if database and database != ":memory:":
                Path(database).parent.mkdir(parents=True, exist_ok=True)
            engine_options["connect_args"] = {"check_same_thread": False}
            engine_options["poolclass"] = NullPool
        elif url.get_backend_name() == "postgresql":
            engine_options.update({
                "pool_size": pool_size,
                "max_overflow": max_overflow,
                "pool_timeout": pool_timeout,
                "connect_args": {"connect_timeout": connect_timeout},
            })
        self.engine: Engine = create_engine(self.url, **engine_options)
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        digest = hashlib.sha256((secret_key or "development-only").encode("utf-8")).digest()
        self._credential_cipher = Fernet(base64.urlsafe_b64encode(digest))

    @property
    def backend(self) -> str:
        return self.engine.url.get_backend_name()

    def initialize(self) -> None:
        """Create only disposable SQLite schemas; PostgreSQL uses Alembic exclusively."""
        if self.backend == "sqlite":
            inspector = inspect(self.engine)
            if not inspector.has_table("alembic_version"):
                Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()

    def healthcheck(self) -> tuple[bool, str]:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            tables = set(inspect(self.engine).get_table_names())
            required = {
                QueryRecord.__tablename__, ModelUsageRecord.__tablename__,
                DocumentRecord.__tablename__, DocumentVersionRecord.__tablename__,
                DocumentChunkRecord.__tablename__, DocumentAclRecord.__tablename__,
                DocumentVersionReviewRecord.__tablename__,
                IngestionJobRecord.__tablename__,
                EvaluationCaseRecord.__tablename__,
                EvaluationRunRecord.__tablename__,
                EvaluationCaseResultRecord.__tablename__,
                AuditEventRecord.__tablename__, RuntimeModelConfigRecord.__tablename__,
                RuntimeProviderCredentialRecord.__tablename__,
            }
            if not required.issubset(tables):
                return False, "database_schema_missing"
            return True, "ok"
        except (OSError, SQLAlchemyError):
            return False, "database_unavailable"

    def record(self, session_id: str, question: str, evidence: str, model_route: str,
               owner_subject_id: str = "legacy") -> int:
        self.initialize()
        row = QueryRecord(
            session_id=str(session_id or "default")[:128],
            owner_subject_id=owner_subject_id[:64],
            question=question,
            evidence=evidence,
            model_route=model_route,
        )
        with self._sessions.begin() as session:
            session.add(row)
        return int(row.id)

    def update_query_route(self, query_id: int, model_route: str, evidence: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(QueryRecord, query_id)
            if row is not None:
                row.model_route = model_route
                row.evidence = evidence

    def history(self, session_id: str, limit: int = 20,
                owner_subject_id: str = "legacy") -> list[dict]:
        self.initialize()
        count = max(1, min(int(limit), 100))
        statement = (
            select(QueryRecord)
            .where(QueryRecord.session_id == str(session_id or "default")[:128])
            .where(QueryRecord.owner_subject_id == owner_subject_id[:64])
            .order_by(QueryRecord.id.desc())
            .limit(count)
        )
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [
            {
                "id": row.id,
                "question": row.question,
                "evidence": row.evidence,
                "model_route": row.model_route,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    def record_model_attempts(self, query_id: int, request_id: str, route: dict, attempts) -> None:
        pricing = pricing_status(route["provider"], route["model"])
        input_price = Decimal(str(pricing["input"])) if pricing["known"] else None
        output_price = Decimal(str(pricing["output"])) if pricing["known"] else None
        rows = []
        for attempt in attempts:
            charge = None
            if attempt.usage_reported and pricing["known"]:
                charge = Decimal(str(cost_cny(
                    route["provider"], route["model"],
                    attempt.prompt_tokens or 0, attempt.completion_tokens or 0,
                )))
            rows.append(ModelUsageRecord(
                query_id=query_id,
                document_version_id=None,
                operation="chat",
                request_id=request_id[:128],
                provider=route["provider"],
                model=route["model"][:128],
                route=route["route"],
                attempt=attempt.attempt,
                status=attempt.status,
                prompt_tokens=attempt.prompt_tokens,
                completion_tokens=attempt.completion_tokens,
                total_tokens=attempt.total_tokens,
                usage_reported=attempt.usage_reported,
                input_price_cny=input_price,
                output_price_cny=output_price,
                cost_cny=charge,
                provider_request_id=attempt.provider_request_id or None,
                latency_ms=attempt.latency_ms,
                error_code=attempt.error_code,
            ))
        if rows:
            with self._sessions.begin() as session:
                session.add_all(rows)

    def record_embedding_usages(self, *, usages, request_id: str,
                                query_id: int | None = None,
                                document_version_id: int | None = None) -> None:
        rows = [ModelUsageRecord(
            query_id=query_id,
            document_version_id=document_version_id,
            operation="embedding",
            request_id=request_id[:128],
            provider=usage.provider,
            model=usage.model[:128],
            route="embedding",
            attempt=index,
            status=usage.status,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=0 if usage.prompt_tokens is not None else None,
            total_tokens=usage.total_tokens,
            usage_reported=usage.usage_reported,
            input_price_cny=None,
            output_price_cny=None,
            cost_cny=None,
            provider_request_id=usage.provider_request_id or None,
            latency_ms=usage.latency_ms,
            error_code=usage.error_code,
        ) for index, usage in enumerate(usages, 1)]
        if rows:
            with self._sessions.begin() as session:
                session.add_all(rows)

    @staticmethod
    def _new_review(*, version_id: int, action: str, from_status: str, to_status: str,
                    actor_subject_id: str = "", comment: str = "", is_override: bool = False,
                    request_id: str = "") -> DocumentVersionReviewRecord:
        return DocumentVersionReviewRecord(
            document_version_id=version_id,
            action=action,
            from_status=from_status,
            to_status=to_status,
            actor_subject_id=(actor_subject_id or "system")[:64],
            comment=(comment or "")[:1000],
            is_override=bool(is_override),
            request_id=(request_id or "")[:128],
            created_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _self_review_override(*, submitted_by: str, actor_subject_id: str,
                              require_separation: bool, allow_override: bool) -> bool:
        """Reject self-review unless the audited override switch allows it.

        Returns True when the recorded decision must be flagged as an override.
        """
        self_review = bool(
            require_separation and actor_subject_id and submitted_by == actor_subject_id
        )
        if self_review and not allow_override:
            raise GovernanceError(
                "separation_of_duties_violation", "提交人不能审核自己提交的版本",
            )
        return self_review

    @staticmethod
    def _version_public(row: DocumentVersionRecord) -> dict:
        return {
            "document_id": row.document_id,
            "version_id": row.id,
            "version": row.version,
            "status": row.status,
            "chunk_count": row.chunk_count,
            "submitted_by_subject_id": row.submitted_by_subject_id,
            "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
            "published_by_subject_id": row.published_by_subject_id,
            "published_at": row.published_at.isoformat() if row.published_at else None,
            "withdrawn_at": row.withdrawn_at.isoformat() if row.withdrawn_at else None,
            "withdrawn_reason": row.withdrawn_reason,
        }

    def _version_row(self, session, document_id: int, version: int) -> DocumentVersionRecord:
        row = session.scalar(select(DocumentVersionRecord).where(
            DocumentVersionRecord.document_id == int(document_id),
            DocumentVersionRecord.version == int(version),
        ))
        if row is None:
            raise ValueError("文档版本不存在")
        return row

    def begin_document_import(self, *, source_key: str, title: str, mime_type: str,
                              content_sha256: str, access_scope: str | None = None,
                              classification: str | None = None,
                              submitted_by_subject_id: str = "",
                              request_id: str = "") -> dict:
        normalized_scope = str(access_scope).strip().lower() if access_scope is not None else None
        if normalized_scope is not None and normalized_scope not in {"public", "restricted"}:
            raise ValueError("文档访问范围无效")
        # Rejected at the boundary so only recognised labels ever reach the database; an
        # unrecognised label would otherwise sit in a row and have to fail closed at read time.
        normalized_classification = (
            normalize_classification(classification) if classification is not None else None
        )
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            document = session.scalar(select(DocumentRecord).where(
                DocumentRecord.source_key == source_key[:512],
            ))
            if document is None:
                document = DocumentRecord(
                    source_key=source_key[:512], title=title[:512], mime_type=mime_type,
                    access_scope=normalized_scope or "public",
                    classification=normalized_classification or INTERNAL,
                    created_at=now, updated_at=now,
                )
                session.add(document)
                session.flush()
            else:
                # Importing must never change who may read an existing document. That decision
                # belongs to the ACL endpoint, which requires acl.write and is audited; otherwise
                # any principal allowed to import could widen a restricted document to public.
                if normalized_scope is not None and normalized_scope != document.access_scope:
                    raise GovernanceError(
                        "access_scope_change_requires_acl",
                        "已存在文档的访问范围不能通过导入修改，请在权限接口中调整",
                    )
                document.title = title[:512]
                document.mime_type = mime_type
                if normalized_classification is not None:
                    # Classification gates reads, so lowering it through an import would be a
                    # privilege-escalation path — the same shape as the access_scope guard above.
                    # Raising it (getting stricter) stays allowed; an unrecognised value already
                    # in the row is treated as the strictest possible, so this never opens a row.
                    current = CLASSIFICATION_RANK.get(
                        str(document.classification or "").strip().lower(), CLASSIFICATION_RANK[CONFIDENTIAL],
                    )
                    if CLASSIFICATION_RANK[normalized_classification] < current:
                        raise GovernanceError(
                            "classification_cannot_be_lowered",
                            "已存在文档的密级只能提升，不能通过导入降低",
                        )
                    document.classification = normalized_classification
                document.updated_at = now
            existing = session.scalar(select(DocumentVersionRecord).where(
                DocumentVersionRecord.document_id == document.id,
                DocumentVersionRecord.content_sha256 == content_sha256,
            ))
            if existing is not None and existing.status in ({"indexed"} | set(IN_FLIGHT_STATUSES)):
                return {
                    "document_id": document.id, "version_id": existing.id,
                    "version": existing.version, "duplicate": True, "reopened": False,
                    "status": existing.status,
                }
            if existing is not None:
                # Re-submitting content that was rejected, withdrawn, superseded or failed is an
                # explicit reopen: it goes back to the queue and must pass review again. The row is
                # reused because uq_document_versions_hash forbids a second row with this content.
                previous_status = existing.status
                existing.status = "queued"
                existing.error_code = None
                existing.submitted_by_subject_id = (submitted_by_subject_id or "")[:64] or None
                existing.submitted_at = now
                existing.withdrawn_at = None
                existing.withdrawn_reason = None
                version = existing
                session.add(self._new_review(
                    version_id=version.id, action="reopen", from_status=previous_status,
                    to_status="queued", actor_subject_id=submitted_by_subject_id,
                    comment=f"重新提交（原状态 {previous_status}）", request_id=request_id,
                ))
            else:
                latest = session.scalar(select(func.max(DocumentVersionRecord.version)).where(
                    DocumentVersionRecord.document_id == document.id,
                )) or 0
                version = DocumentVersionRecord(
                    document_id=document.id,
                    version=int(latest) + 1,
                    content_sha256=content_sha256,
                    status="queued",
                    chunk_count=0,
                    created_at=now,
                    submitted_by_subject_id=(submitted_by_subject_id or "")[:64] or None,
                    submitted_at=now,
                )
                session.add(version)
                session.flush()
                session.add(self._new_review(
                    version_id=version.id, action="submit", from_status="none",
                    to_status="queued", actor_subject_id=submitted_by_subject_id,
                    request_id=request_id,
                ))
            session.flush()
            return {
                "document_id": document.id, "version_id": version.id,
                "version": version.version, "duplicate": False,
                "reopened": existing is not None, "status": version.status,
            }

    def persist_document_chunks(self, version_id: int, chunks, vectors) -> int:
        """Replace every chunk of a version. Used by the synchronous indexing path."""
        if len(chunks) != len(vectors):
            raise ValueError("文档块和向量数量不一致")
        if not chunks:
            raise ValueError("文档没有可索引的文本内容")
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            version = session.get(DocumentVersionRecord, version_id)
            if version is None:
                raise ValueError("文档版本不存在")
            if version.status not in {"queued", "processing"}:
                raise GovernanceError("invalid_state_transition", "该版本不处于可索引状态")
            session.query(DocumentChunkRecord).filter(
                DocumentChunkRecord.document_version_id == version_id,
            ).delete()
            session.add_all([
                DocumentChunkRecord(
                    document_version_id=version_id,
                    ordinal=chunk.ordinal,
                    heading=chunk.heading[:512],
                    page_number=chunk.page_number,
                    content=chunk.content,
                    search_text=lexical_text(f"{chunk.heading} {chunk.content}"),
                    embedding=list(vector),
                    created_at=now,
                )
                for chunk, vector in zip(chunks, vectors)
            ])
            session.flush()
            return len(chunks)

    def replace_document_chunk_batch(self, version_id: int, rows) -> int:
        """Idempotently write one batch of chunks (ordinal, heading, page, content, embedding).

        Delete-then-insert on the batch's own ordinals, so a resumed or repeated batch can never
        duplicate rows even though the checkpoint write and this write are separate transactions.
        """
        if not rows:
            return 0
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            version = session.get(DocumentVersionRecord, version_id)
            if version is None:
                raise ValueError("文档版本不存在")
            if version.status not in {"queued", "processing"}:
                raise GovernanceError("invalid_state_transition", "该版本不处于可索引状态")
            ordinals = [int(row["ordinal"]) for row in rows]
            session.query(DocumentChunkRecord).filter(
                DocumentChunkRecord.document_version_id == version_id,
                DocumentChunkRecord.ordinal.in_(ordinals),
            ).delete(synchronize_session=False)
            session.add_all([
                DocumentChunkRecord(
                    document_version_id=version_id,
                    ordinal=int(row["ordinal"]),
                    heading=str(row.get("heading") or "")[:512],
                    page_number=row.get("page_number"),
                    content=str(row.get("content") or ""),
                    search_text=lexical_text(f"{row.get('heading') or ''} {row.get('content') or ''}"),
                    embedding=list(row["embedding"]),
                    created_at=now,
                )
                for row in rows
            ])
            session.flush()
            return len(rows)

    def clear_document_version_chunks(self, version_id: int) -> int:
        """Drop staged chunks of a version so a fresh indexing run starts clean."""
        with self._sessions.begin() as session:
            deleted = session.query(DocumentChunkRecord).filter(
                DocumentChunkRecord.document_version_id == version_id,
            ).delete(synchronize_session=False)
            return int(deleted or 0)

    def count_document_chunks(self, version_id: int) -> int:
        with self._sessions() as session:
            return int(session.scalar(
                select(func.count()).select_from(DocumentChunkRecord).where(
                    DocumentChunkRecord.document_version_id == version_id,
                )
            ) or 0)

    def finalize_document_version(self, version_id: int, *, chunk_count: int, publish: bool,
                                  actor_subject_id: str = "", comment: str = "",
                                  request_id: str = "") -> dict:
        """Move an indexed version to ``staged`` or straight to ``indexed``.

        ``staged`` is deliberately not retrievable: only ``indexed`` appears in any search path.
        """
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            version = session.get(DocumentVersionRecord, version_id)
            if version is None:
                raise ValueError("文档版本不存在")
            if version.status not in {"queued", "processing"}:
                raise GovernanceError("invalid_state_transition", "该版本不处于可索引状态")
            from_status = version.status
            version.chunk_count = int(chunk_count)
            version.error_code = None
            version.indexed_at = now
            if publish:
                for other in session.scalars(select(DocumentVersionRecord).where(
                    DocumentVersionRecord.document_id == version.document_id,
                    DocumentVersionRecord.id != version.id,
                    DocumentVersionRecord.status == "indexed",
                )).all():
                    other.status = "superseded"
                    other.superseded_by_version_id = version.id
                version.status = "indexed"
                version.published_at = now
                version.published_by_subject_id = (actor_subject_id or "system")[:64]
                version.withdrawn_at = None
                version.withdrawn_reason = None
                session.add(self._new_review(
                    version_id=version.id, action="publish", from_status=from_status,
                    to_status="indexed", actor_subject_id=actor_subject_id, comment=comment,
                    request_id=request_id,
                ))
            else:
                version.status = "staged"
            session.flush()
            return self._version_public(version)

    def finalize_document_indexing(self, version_id: int, chunks, vectors, *, publish: bool,
                                   actor_subject_id: str = "", comment: str = "",
                                   request_id: str = "") -> dict:
        """Synchronous composition: persist every chunk, then finalize the version."""
        chunk_count = self.persist_document_chunks(version_id, chunks, vectors)
        return self.finalize_document_version(
            version_id, chunk_count=chunk_count, publish=publish,
            actor_subject_id=actor_subject_id, comment=comment, request_id=request_id,
        )

    def complete_document_import(self, version_id: int, chunks, vectors) -> None:
        """Direct-mode entry point: indexing completes and the version is published at once."""
        self.finalize_document_indexing(version_id, chunks, vectors, publish=True)

    def fail_document_import(self, version_id: int, error_code: str) -> None:
        with self._sessions.begin() as session:
            version = session.get(DocumentVersionRecord, version_id)
            if version is not None:
                version.status = "failed"
                version.error_code = error_code[:64]

    def document_version_pk(self, document_id: int, version: int) -> int:
        """Resolve (document_id, version number) to the version primary key."""
        with self._sessions() as session:
            return int(self._version_row(session, document_id, version).id)

    def document_version_reference(self, version_id: int) -> dict:
        """Resolve a version id to the document id and version number (for the worker)."""
        with self._sessions() as session:
            row = session.get(DocumentVersionRecord, int(version_id))
            if row is None:
                raise ValueError("文档版本不存在")
            return {
                "document_id": row.document_id,
                "version": row.version,
                "status": row.status,
                "document_version_id": row.id,
            }

    def mark_document_version_processing(self, version_id: int, *, title: str = "",
                                         mime_type: str = "") -> dict:
        """Claim a queued version for processing and refine its document metadata."""
        with self._sessions.begin() as session:
            version = session.get(DocumentVersionRecord, version_id)
            if version is None:
                raise ValueError("文档版本不存在")
            if version.status not in {"queued", "processing"}:
                raise GovernanceError("invalid_state_transition", "该版本不处于可处理状态")
            version.status = "processing"
            if title or mime_type:
                document = session.get(DocumentRecord, version.document_id)
                if document is not None:
                    if title:
                        document.title = title[:512]
                    if mime_type:
                        document.mime_type = mime_type[:128]
                    document.updated_at = datetime.now(timezone.utc)
            session.flush()
            return self._version_public(version)

    def reset_document_version_for_retry(self, version_id: int) -> None:
        """Return a version to the queue so a retried job can index it again."""
        with self._sessions.begin() as session:
            version = session.get(DocumentVersionRecord, version_id)
            if version is not None and version.status in {"processing", "failed"}:
                version.status = "queued"
                version.error_code = None

    @staticmethod
    def _job_public(row: IngestionJobRecord) -> dict:
        return {
            "job_id": row.id,
            "job_type": row.job_type,
            "status": row.status,
            "priority": row.priority,
            "document_id": row.document_id,
            "version_id": row.version_id,
            "attempts": row.attempts,
            "max_attempts": row.max_attempts,
            "last_error_code": row.last_error_code,
            "locked_by": row.locked_by,
            "created_by_subject_id": row.created_by_subject_id,
            "request_id": row.request_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "heartbeat_at": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
        }

    def enqueue_ingestion_job(self, *, job_type: str, document_id: int | None = None,
                              version_id: int | None = None, payload: dict | None = None,
                              created_by_subject_id: str = "", request_id: str = "",
                              max_attempts: int = 3, priority: int = 100) -> dict:
        """Create the job for a target, or re-queue the existing one.

        One row per ``(job_type, version_id)`` keeps repeated submissions of the same version from
        flooding the queue; re-queueing resets the attempt counter so a manual retry is a real try.
        """
        if job_type not in {"import", "reindex", "withdraw", "evaluate"}:
            raise ValueError("不支持的任务类型")
        attempts_limit = max(1, int(max_attempts))
        with self._sessions.begin() as session:
            existing = None
            if version_id is not None:
                existing = session.scalar(select(IngestionJobRecord).where(
                    IngestionJobRecord.job_type == job_type,
                    IngestionJobRecord.version_id == int(version_id),
                ))
            if existing is not None:
                if existing.status == "running":
                    raise GovernanceError("job_already_running", "该版本已有任务正在运行")
                existing.status = "queued"
                existing.attempts = 0
                existing.max_attempts = attempts_limit
                existing.last_error_code = None
                existing.locked_by = None
                existing.locked_at = None
                existing.heartbeat_at = None
                existing.started_at = None
                existing.finished_at = None
                existing.payload = dict(payload or {})
                existing.request_id = (request_id or "")[:128]
                if created_by_subject_id:
                    existing.created_by_subject_id = created_by_subject_id[:64]
                session.flush()
                return self._job_public(existing)
            job = IngestionJobRecord(
                job_type=job_type,
                status="queued",
                priority=int(priority),
                document_id=int(document_id) if document_id is not None else None,
                version_id=int(version_id) if version_id is not None else None,
                payload=dict(payload or {}),
                attempts=0,
                max_attempts=attempts_limit,
                created_by_subject_id=(created_by_subject_id or "")[:64] or None,
                request_id=(request_id or "")[:128],
                created_at=datetime.now(timezone.utc),
            )
            session.add(job)
            session.flush()
            return self._job_public(job)

    def claim_ingestion_job(self, worker_id: str) -> dict | None:
        """Atomically take the next queued job for this worker.

        PostgreSQL uses ``FOR UPDATE SKIP LOCKED`` so several workers never take the same job.
        SQLite has no such clause; the portable branch performs a conditional update and treats a
        zero row count as "someone else won the race".
        """
        now = datetime.now(timezone.utc)
        worker = (worker_id or "worker")[:64]
        with self._sessions.begin() as session:
            if self.backend == "postgresql":
                claimed_id = session.execute(text("""
                    UPDATE ingestion_jobs
                       SET status = 'running', attempts = attempts + 1,
                           locked_by = :worker, locked_at = :now,
                           heartbeat_at = :now, started_at = COALESCE(started_at, :now)
                     WHERE id = (
                         SELECT id FROM ingestion_jobs
                          WHERE status = 'queued' AND attempts < max_attempts
                          ORDER BY priority, id
                          FOR UPDATE SKIP LOCKED
                          LIMIT 1
                     )
                 RETURNING id
                """), {"worker": worker, "now": now}).scalar()
                if claimed_id is None:
                    return None
                job_id = int(claimed_id)
            else:
                candidate = session.scalar(
                    select(IngestionJobRecord.id)
                    .where(
                        IngestionJobRecord.status == "queued",
                        IngestionJobRecord.attempts < IngestionJobRecord.max_attempts,
                    )
                    .order_by(IngestionJobRecord.priority, IngestionJobRecord.id)
                    .limit(1)
                )
                if candidate is None:
                    return None
                claimed = session.execute(
                    update(IngestionJobRecord)
                    .where(
                        IngestionJobRecord.id == candidate,
                        IngestionJobRecord.status == "queued",
                    )
                    .values(
                        status="running",
                        attempts=IngestionJobRecord.attempts + 1,
                        locked_by=worker,
                        locked_at=now,
                        heartbeat_at=now,
                        started_at=func.coalesce(IngestionJobRecord.started_at, now),
                    )
                ).rowcount
                if not claimed:
                    return None
                job_id = int(candidate)
            job = session.get(IngestionJobRecord, job_id)
            return self._job_public(job) if job is not None else None

    def heartbeat_ingestion_job(self, job_id: int, worker_id: str = "") -> bool:
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            conditions = [
                IngestionJobRecord.id == int(job_id),
                IngestionJobRecord.status == "running",
            ]
            if worker_id:
                conditions.append(IngestionJobRecord.locked_by == worker_id[:64])
            updated = session.execute(
                update(IngestionJobRecord).where(*conditions).values(heartbeat_at=now)
            ).rowcount
            return bool(updated)

    def complete_ingestion_job(self, job_id: int, worker_id: str = "") -> bool:
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            conditions = [
                IngestionJobRecord.id == int(job_id),
                IngestionJobRecord.status == "running",
            ]
            if worker_id:
                conditions.append(IngestionJobRecord.locked_by == worker_id[:64])
            updated = session.execute(
                update(IngestionJobRecord).where(*conditions).values(
                    status="succeeded", finished_at=now, locked_by=None,
                    locked_at=None, heartbeat_at=now, last_error_code=None,
                )
            ).rowcount
            return bool(updated)

    def fail_ingestion_job(self, job_id: int, error_code: str, *,
                           retryable: bool) -> dict:
        """Record a failure and decide between another attempt and a terminal failure."""
        now = datetime.now(timezone.utc)
        code = (error_code or "job_failed")[:64]
        with self._sessions.begin() as session:
            job = session.get(IngestionJobRecord, int(job_id))
            if job is None:
                raise ValueError("任务不存在")
            can_retry = bool(retryable and job.attempts < job.max_attempts)
            job.last_error_code = code
            job.locked_by = None
            job.locked_at = None
            job.heartbeat_at = None
            if can_retry:
                job.status = "queued"
            else:
                job.status = "failed"
                job.finished_at = now
            session.flush()
            return self._job_public(job)

    def reclaim_stale_ingestion_jobs(self, *, timeout_seconds: int = 600) -> int:
        """Return abandoned ``running`` jobs to the queue, or fail them when out of attempts."""
        now = datetime.now(timezone.utc)
        deadline = now - timedelta(seconds=max(30, int(timeout_seconds)))
        with self._sessions.begin() as session:
            stale = session.scalars(select(IngestionJobRecord).where(
                IngestionJobRecord.status == "running",
                IngestionJobRecord.heartbeat_at < deadline,
            )).all()
            reclaimed = 0
            for job in stale:
                job.last_error_code = job.last_error_code or "job_abandoned"
                job.locked_by = None
                job.locked_at = None
                job.heartbeat_at = None
                if job.attempts < job.max_attempts:
                    job.status = "queued"
                else:
                    job.status = "failed"
                    job.finished_at = now
                reclaimed += 1
            session.flush()
            return reclaimed

    def list_ingestion_jobs(self, limit: int = 50, status: str | None = None) -> list[dict]:
        statement = (
            select(IngestionJobRecord, DocumentRecord, DocumentVersionRecord)
            .outerjoin(DocumentRecord, DocumentRecord.id == IngestionJobRecord.document_id)
            .outerjoin(
                DocumentVersionRecord, DocumentVersionRecord.id == IngestionJobRecord.version_id,
            )
        )
        if status:
            if status not in JOB_STATUSES:
                raise ValueError("任务状态无效")
            statement = statement.where(IngestionJobRecord.status == status)
        statement = statement.order_by(IngestionJobRecord.id.desc()).limit(
            max(1, min(int(limit), 200)),
        )
        with self._sessions() as session:
            rows = session.execute(statement).all()
        return [{
            **self._job_public(job),
            "title": document.title if document is not None else None,
            "source_key": document.source_key if document is not None else None,
            "version": version.version if version is not None else None,
            "version_status": version.status if version is not None else None,
        } for job, document, version in rows]

    def retry_ingestion_job(self, job_id: int, *, actor_subject_id: str = "") -> dict:
        with self._sessions.begin() as session:
            job = session.get(IngestionJobRecord, int(job_id))
            if job is None:
                raise ValueError("任务不存在")
            if job.status in ACTIVE_JOB_STATUSES:
                raise GovernanceError("job_already_active", "任务正在排队或运行，无需重试")
            job.status = "queued"
            job.attempts = 0
            job.last_error_code = None
            job.locked_by = None
            job.locked_at = None
            job.heartbeat_at = None
            job.started_at = None
            job.finished_at = None
            if actor_subject_id:
                job.request_id = f"retry-by-{actor_subject_id[:32]}-{job.request_id}"[:128]
            session.flush()
            return self._job_public(job)

    def cancel_ingestion_job(self, job_id: int, *, actor_subject_id: str = "") -> dict:
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            job = session.get(IngestionJobRecord, int(job_id))
            if job is None:
                raise ValueError("任务不存在")
            if job.status != "queued":
                raise GovernanceError("job_not_cancellable", "只能取消尚未开始的任务")
            job.status = "cancelled"
            job.finished_at = now
            job.last_error_code = "cancelled"
            if actor_subject_id:
                job.request_id = f"cancel-by-{actor_subject_id[:32]}-{job.request_id}"[:128]
            session.flush()
            return self._job_public(job)

    def ingestion_queue_stats(self, *, timeout_seconds: int = 600) -> dict:
        now = datetime.now(timezone.utc)
        deadline = now - timedelta(seconds=max(30, int(timeout_seconds)))
        with self._sessions() as session:
            counts = {
                status: int(count or 0)
                for status, count in session.execute(
                    select(IngestionJobRecord.status, func.count()).group_by(
                        IngestionJobRecord.status
                    )
                ).all()
            }
            stale_running = int(session.scalar(
                select(func.count()).select_from(IngestionJobRecord).where(
                    IngestionJobRecord.status == "running",
                    IngestionJobRecord.heartbeat_at < deadline,
                )
            ) or 0)
            stale_queued = int(session.scalar(
                select(func.count()).select_from(IngestionJobRecord).where(
                    IngestionJobRecord.status == "queued",
                    IngestionJobRecord.created_at < deadline,
                )
            ) or 0)
        return {
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "succeeded": counts.get("succeeded", 0),
            "failed": counts.get("failed", 0),
            "cancelled": counts.get("cancelled", 0),
            "stale_running": stale_running,
            "stale_queued": stale_queued,
        }

    def pending_review_versions(self, limit: int = 100) -> list[dict]:
        """Versions waiting for a review decision; never retrievable until published."""
        statement = (
            select(DocumentRecord, DocumentVersionRecord)
            .join(DocumentVersionRecord, DocumentVersionRecord.document_id == DocumentRecord.id)
            .where(DocumentVersionRecord.status == "staged")
            .order_by(DocumentVersionRecord.submitted_at, DocumentVersionRecord.id)
            .limit(max(1, min(int(limit), 200)))
        )
        with self._sessions() as session:
            rows = session.execute(statement).all()
        return [{
            "document_id": document.id,
            "title": document.title,
            "source_key": document.source_key,
            "access_scope": document.access_scope,
            "classification": document.classification,
            "version": version.version,
            "chunk_count": version.chunk_count,
            "submitted_by_subject_id": version.submitted_by_subject_id,
            "submitted_at": version.submitted_at.isoformat() if version.submitted_at else None,
            "indexed_at": version.indexed_at.isoformat() if version.indexed_at else None,
        } for document, version in rows]

    def document_version_reviews(self, document_id: int, version: int,
                                 limit: int = 100) -> list[dict]:
        with self._sessions() as session:
            row = self._version_row(session, document_id, version)
            records = session.scalars(
                select(DocumentVersionReviewRecord)
                .where(DocumentVersionReviewRecord.document_version_id == row.id)
                .order_by(DocumentVersionReviewRecord.id.desc())
                .limit(max(1, min(int(limit), 200)))
            ).all()
        return [{
            "id": item.id,
            "action": item.action,
            "from_status": item.from_status,
            "to_status": item.to_status,
            "actor_subject_id": item.actor_subject_id,
            "comment": item.comment,
            "is_override": item.is_override,
            "request_id": item.request_id,
            "created_at": item.created_at.isoformat(),
        } for item in records]

    def document_version_chunks(self, document_id: int, version: int,
                               limit: int = 200) -> list[dict]:
        """Reviewer preview of one version. Deliberately bypasses ACL, so callers must require a
        review capability and write an audit event for every read."""
        with self._sessions() as session:
            row = self._version_row(session, document_id, version)
            chunks = session.scalars(
                select(DocumentChunkRecord)
                .where(DocumentChunkRecord.document_version_id == row.id)
                .order_by(DocumentChunkRecord.ordinal)
                .limit(max(1, min(int(limit), 500)))
            ).all()
            return {
                "version": self._version_public(row),
                "chunks": [{
                    "ordinal": chunk.ordinal,
                    "heading": chunk.heading,
                    "page": chunk.page_number,
                    "content": chunk.content,
                } for chunk in chunks],
            }

    def review_document_version(self, *, document_id: int, version: int, decision: str,
                                actor_subject_id: str, comment: str = "", request_id: str = "",
                                require_separation_of_duties: bool = True,
                                allow_override: bool = False) -> dict:
        normalized = (decision or "").strip().lower()
        if normalized not in {"approve", "reject"}:
            raise GovernanceError("invalid_review_decision", "审核结论只能是 approve 或 reject")
        cleaned = (comment or "").strip()
        if normalized == "reject" and not cleaned:
            raise GovernanceError("review_comment_required", "驳回必须填写意见")
        with self._sessions.begin() as session:
            row = self._version_row(session, document_id, version)
            if row.status != "staged":
                raise GovernanceError("invalid_state_transition", "只有待审核版本可以审核")
            is_override = self._self_review_override(
                submitted_by=row.submitted_by_subject_id or "", actor_subject_id=actor_subject_id,
                require_separation=require_separation_of_duties, allow_override=allow_override,
            )
            if normalized == "approve":
                # Approval authorises publication; it does not publish. The status is unchanged so
                # the publish action stays an explicit, separately attributable decision.
                session.add(self._new_review(
                    version_id=row.id, action="approve", from_status="staged", to_status="staged",
                    actor_subject_id=actor_subject_id, comment=cleaned, is_override=is_override,
                    request_id=request_id,
                ))
            else:
                row.status = "rejected"
                session.add(self._new_review(
                    version_id=row.id, action="reject", from_status="staged", to_status="rejected",
                    actor_subject_id=actor_subject_id, comment=cleaned, is_override=is_override,
                    request_id=request_id,
                ))
            session.flush()
            return self._version_public(row)

    def publish_document_version(self, *, document_id: int, version: int, actor_subject_id: str,
                                 comment: str = "", request_id: str = "",
                                 allow_override: bool = False, gate_override: bool = False,
                                 gate_comment: str = "") -> dict:
        with self._sessions.begin() as session:
            row = self._version_row(session, document_id, version)
            if row.status != "staged":
                raise GovernanceError("invalid_state_transition", "只有待审核版本可以发布")
            approvals = session.scalar(
                select(func.count()).select_from(DocumentVersionReviewRecord).where(
                    DocumentVersionReviewRecord.document_version_id == row.id,
                    DocumentVersionReviewRecord.action == "approve",
                )
            ) or 0
            if not approvals and not allow_override:
                raise GovernanceError("review_approval_required", "发布前必须先通过审核")
            is_override = not approvals
            now = datetime.now(timezone.utc)
            if gate_override:
                # Recorded before the publish record so the trail reads: bypass, then publish.
                session.add(self._new_review(
                    version_id=row.id, action="override_gate", from_status="staged",
                    to_status="staged", actor_subject_id=actor_subject_id,
                    comment=(gate_comment or "").strip(), is_override=True,
                    request_id=request_id,
                ))
            for other in session.scalars(select(DocumentVersionRecord).where(
                DocumentVersionRecord.document_id == row.document_id,
                DocumentVersionRecord.id != row.id,
                DocumentVersionRecord.status == "indexed",
            )).all():
                other.status = "superseded"
                other.superseded_by_version_id = row.id
            row.status = "indexed"
            row.indexed_at = row.indexed_at or now
            row.published_at = now
            row.published_by_subject_id = (actor_subject_id or "system")[:64]
            row.withdrawn_at = None
            row.withdrawn_reason = None
            session.add(self._new_review(
                version_id=row.id, action="publish", from_status="staged", to_status="indexed",
                actor_subject_id=actor_subject_id, comment=(comment or "").strip(),
                is_override=is_override, request_id=request_id,
            ))
            session.flush()
            return self._version_public(row)

    def withdraw_document_version(self, *, document_id: int, version: int, actor_subject_id: str,
                                  reason: str, request_id: str = "") -> dict:
        cleaned = (reason or "").strip()
        if not cleaned:
            raise GovernanceError("withdraw_reason_required", "作废必须填写原因")
        with self._sessions.begin() as session:
            row = self._version_row(session, document_id, version)
            if row.status != "indexed":
                raise GovernanceError("invalid_state_transition", "只能作废当前已发布的版本")
            row.status = "withdrawn"
            row.withdrawn_at = datetime.now(timezone.utc)
            row.withdrawn_reason = cleaned[:512]
            session.add(self._new_review(
                version_id=row.id, action="withdraw", from_status="indexed", to_status="withdrawn",
                actor_subject_id=actor_subject_id, comment=cleaned, request_id=request_id,
            ))
            session.flush()
            return self._version_public(row)

    def rollback_document_version(self, *, document_id: int, version: int, actor_subject_id: str,
                                  reason: str, request_id: str = "") -> dict:
        cleaned = (reason or "").strip()
        if not cleaned:
            raise GovernanceError("rollback_reason_required", "回滚必须填写原因")
        with self._sessions.begin() as session:
            target = self._version_row(session, document_id, version)
            if target.status not in {"superseded", "withdrawn"}:
                raise GovernanceError("invalid_state_transition", "只能回滚到曾经发布过的历史版本")
            previous_status = target.status
            now = datetime.now(timezone.utc)
            for other in session.scalars(select(DocumentVersionRecord).where(
                DocumentVersionRecord.document_id == target.document_id,
                DocumentVersionRecord.id != target.id,
                DocumentVersionRecord.status == "indexed",
            )).all():
                other.status = "superseded"
                other.superseded_by_version_id = target.id
            # Historical chunks are kept, so a rollback never re-runs embeddings.
            target.status = "indexed"
            target.published_at = now
            target.published_by_subject_id = (actor_subject_id or "system")[:64]
            target.withdrawn_at = None
            target.withdrawn_reason = None
            target.superseded_by_version_id = None
            session.add(self._new_review(
                version_id=target.id, action="rollback", from_status=previous_status,
                to_status="indexed", actor_subject_id=actor_subject_id, comment=cleaned,
                request_id=request_id,
            ))
            session.flush()
            return self._version_public(target)

    # -- evaluation gate ------------------------------------------------------
    @staticmethod
    def _case_public(row: EvaluationCaseRecord) -> dict:
        return {
            "case_id": row.id,
            "case_key": row.case_key,
            "question": row.question,
            "expect_refusal": bool(row.expect_refusal),
            "expected_document_key": row.expected_document_key,
            "expected_heading": row.expected_heading,
            "tags": row.tags,
            "active": bool(row.active),
            "created_by_subject_id": row.created_by_subject_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _run_public(row: EvaluationRunRecord) -> dict:
        def ratio(value):
            return float(value) if value is not None else None

        return {
            "run_id": row.id,
            "trigger": row.trigger,
            "status": row.status,
            "document_version_id": row.document_version_id,
            "gate_mode": row.gate_mode,
            "gate_result": row.gate_result,
            "gate_reason": row.gate_reason,
            "total_cases": row.total_cases,
            "passed_cases": row.passed_cases,
            "failed_cases": row.failed_cases,
            "recall_at_k": ratio(row.recall_at_k),
            "citation_accuracy": ratio(row.citation_accuracy),
            "refusal_accuracy": ratio(row.refusal_accuracy),
            "baseline_run_id": row.baseline_run_id,
            "created_by_subject_id": row.created_by_subject_id,
            "request_id": row.request_id,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "error_code": row.error_code,
        }

    def list_evaluation_cases(self, *, active_only: bool = False,
                              limit: int = 200) -> list[dict]:
        statement = select(EvaluationCaseRecord)
        if active_only:
            statement = statement.where(EvaluationCaseRecord.active.is_(True))
        statement = statement.order_by(EvaluationCaseRecord.case_key).limit(
            max(1, min(int(limit), 500)),
        )
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [self._case_public(row) for row in rows]

    def upsert_evaluation_case(self, *, case_key: str, question: str, expect_refusal: bool = False,
                               expected_document_key: str | None = None,
                               expected_heading: str | None = None, tags: str = "",
                               active: bool = True, actor_subject_id: str = "") -> dict:
        key = (case_key or "").strip()[:64]
        text = (question or "").strip()
        if not key or not text:
            raise ValueError("评测用例必须包含 case_key 与 question")
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            row = session.scalar(select(EvaluationCaseRecord).where(
                EvaluationCaseRecord.case_key == key,
            ))
            if row is None:
                row = EvaluationCaseRecord(
                    case_key=key, question=text, expect_refusal=bool(expect_refusal),
                    expected_document_key=(expected_document_key or None),
                    expected_heading=(expected_heading or None),
                    tags=(tags or "")[:256], active=bool(active),
                    created_by_subject_id=(actor_subject_id or "")[:64] or None,
                    created_at=now, updated_at=now,
                )
                session.add(row)
            else:
                row.question = text
                row.expect_refusal = bool(expect_refusal)
                row.expected_document_key = expected_document_key or None
                row.expected_heading = expected_heading or None
                row.tags = (tags or "")[:256]
                row.active = bool(active)
                row.updated_at = now
            session.flush()
            return self._case_public(row)

    def delete_evaluation_case(self, case_id: int) -> None:
        with self._sessions.begin() as session:
            row = session.get(EvaluationCaseRecord, int(case_id))
            if row is None:
                raise ValueError("评测用例不存在")
            referenced = session.scalar(
                select(func.count()).select_from(EvaluationCaseResultRecord).where(
                    EvaluationCaseResultRecord.case_id == row.id,
                )
            ) or 0
            if referenced:
                # History is evidence: deactivate instead of deleting a case that was measured.
                raise GovernanceError(
                    "evaluation_case_in_use", "该用例已有历史结果，请改为停用而不是删除",
                )
            session.delete(row)

    def create_evaluation_run(self, *, trigger: str, gate_mode: str,
                              document_version_id: int | None = None,
                              actor_subject_id: str = "", request_id: str = "") -> dict:
        if trigger not in {"manual", "pre_publish", "scheduled"}:
            raise ValueError("评测触发方式无效")
        if gate_mode not in {"off", "warn", "block"}:
            raise ValueError("评测门模式无效")
        with self._sessions.begin() as session:
            row = EvaluationRunRecord(
                trigger=trigger, status="running", gate_mode=gate_mode,
                document_version_id=(
                    int(document_version_id) if document_version_id is not None else None
                ),
                gate_reason="", created_by_subject_id=(actor_subject_id or "")[:64] or None,
                request_id=(request_id or "")[:128],
                started_at=datetime.now(timezone.utc),
            )
            session.add(row)
            session.flush()
            return self._run_public(row)

    def complete_evaluation_run(self, run_id: int, *, metrics: dict, results: list[dict],
                                gate_result: str | None, gate_reason: str = "",
                                baseline_run_id: int | None = None) -> dict:
        if gate_result is not None and gate_result not in {
            "pass", "warn", "block", "overridden",
        }:
            raise ValueError("评测门结论无效")
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            row = session.get(EvaluationRunRecord, int(run_id))
            if row is None:
                raise ValueError("评测运行不存在")
            row.status = "succeeded"
            row.finished_at = now
            row.total_cases = int(metrics.get("total_cases") or 0)
            row.passed_cases = int(metrics.get("passed_cases") or 0)
            row.failed_cases = int(metrics.get("failed_cases") or 0)
            row.recall_at_k = metrics.get("recall_at_k")
            row.citation_accuracy = metrics.get("citation_accuracy")
            row.refusal_accuracy = metrics.get("refusal_accuracy")
            row.gate_result = gate_result
            row.gate_reason = (gate_reason or "")[:512]
            row.baseline_run_id = int(baseline_run_id) if baseline_run_id else None
            session.add_all([
                EvaluationCaseResultRecord(
                    run_id=row.id,
                    case_id=int(item["case_id"]),
                    retrieved=bool(item.get("retrieved")),
                    matched_rank=item.get("matched_rank"),
                    citation_ok=item.get("citation_ok"),
                    refusal_ok=item.get("refusal_ok"),
                    latency_ms=int(item.get("latency_ms") or 0),
                    detail=dict(item.get("detail") or {}),
                )
                for item in results
            ])
            session.flush()
            return self._run_public(row)

    def fail_evaluation_run(self, run_id: int, error_code: str) -> dict:
        with self._sessions.begin() as session:
            row = session.get(EvaluationRunRecord, int(run_id))
            if row is None:
                raise ValueError("评测运行不存在")
            row.status = "failed"
            row.error_code = (error_code or "evaluation_failed")[:64]
            row.finished_at = datetime.now(timezone.utc)
            session.flush()
            return self._run_public(row)

    def list_evaluation_runs(self, *, limit: int = 50,
                             document_version_id: int | None = None) -> list[dict]:
        statement = select(EvaluationRunRecord)
        if document_version_id is not None:
            statement = statement.where(
                EvaluationRunRecord.document_version_id == int(document_version_id),
            )
        statement = statement.order_by(EvaluationRunRecord.id.desc()).limit(
            max(1, min(int(limit), 200)),
        )
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [self._run_public(row) for row in rows]

    def evaluation_run_detail(self, run_id: int) -> dict:
        with self._sessions() as session:
            row = session.get(EvaluationRunRecord, int(run_id))
            if row is None:
                raise ValueError("评测运行不存在")
            results = session.execute(
                select(EvaluationCaseResultRecord, EvaluationCaseRecord)
                .join(EvaluationCaseRecord, EvaluationCaseRecord.id == EvaluationCaseResultRecord.case_id)
                .where(EvaluationCaseResultRecord.run_id == row.id)
                .order_by(EvaluationCaseResultRecord.id)
            ).all()
        return {
            **self._run_public(row),
            "results": [{
                "case_key": case.case_key,
                "question": case.question,
                "expect_refusal": bool(case.expect_refusal),
                "expected_document_key": case.expected_document_key,
                "retrieved": bool(result.retrieved),
                "matched_rank": result.matched_rank,
                "citation_ok": result.citation_ok,
                "refusal_ok": result.refusal_ok,
                "latency_ms": result.latency_ms,
                "detail": dict(result.detail or {}),
            } for result, case in results],
        }

    def latest_evaluation_run(self, *, document_version_id: int | None = None,
                              trigger: str | None = None,
                              succeeded_only: bool = True) -> dict | None:
        statement = select(EvaluationRunRecord)
        if document_version_id is not None:
            statement = statement.where(
                EvaluationRunRecord.document_version_id == int(document_version_id),
            )
        if trigger:
            statement = statement.where(EvaluationRunRecord.trigger == trigger)
        if succeeded_only:
            statement = statement.where(EvaluationRunRecord.status == "succeeded")
        statement = statement.order_by(EvaluationRunRecord.id.desc()).limit(1)
        with self._sessions() as session:
            row = session.scalar(statement)
        return self._run_public(row) if row is not None else None

    def list_documents(self) -> list[dict]:
        statement = (
            select(DocumentRecord, DocumentVersionRecord)
            .join(DocumentVersionRecord, DocumentVersionRecord.document_id == DocumentRecord.id)
            .order_by(DocumentRecord.id, DocumentVersionRecord.version.desc())
        )
        with self._sessions() as session:
            rows = session.execute(statement).all()
        return [{
            "document_id": document.id,
            "title": document.title,
            "source_key": document.source_key,
            "mime_type": document.mime_type,
            "access_scope": document.access_scope,
            "classification": document.classification,
            "version": version.version,
            "status": version.status,
            "chunk_count": version.chunk_count,
            "created_at": version.created_at.isoformat(),
            "submitted_by_subject_id": version.submitted_by_subject_id,
            "submitted_at": version.submitted_at.isoformat() if version.submitted_at else None,
            "indexed_at": version.indexed_at.isoformat() if version.indexed_at else None,
            "published_by_subject_id": version.published_by_subject_id,
            "published_at": version.published_at.isoformat() if version.published_at else None,
            "withdrawn_at": version.withdrawn_at.isoformat() if version.withdrawn_at else None,
            "withdrawn_reason": version.withdrawn_reason,
            "error_code": version.error_code,
        } for document, version in rows]

    def document_usage_ledger(self, version_id: int) -> list[dict]:
        statement = (
            select(ModelUsageRecord)
            .where(ModelUsageRecord.document_version_id == version_id)
            .order_by(ModelUsageRecord.id)
        )
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [self._usage_public(row) for row in rows]

    def has_indexed_chunks(self) -> bool:
        statement = (
            select(func.count(DocumentChunkRecord.id))
            .join(DocumentVersionRecord)
            .where(DocumentVersionRecord.status == "indexed")
        )
        with self._sessions() as session:
            return bool(session.scalar(statement))

    def accessible_document_outline(self, *, subject_id: str = "legacy", roles=(), groups=(),
                                    max_documents: int = 20, max_sections: int = 8,
                                    allow_confidential: bool = False) -> list[dict]:
        """Return an ACL-filtered outline without exposing document contents.

        Titles and headings are withheld for classifications the caller cannot read: a
        confidential document's title alone can be sensitive, and this path feeds the
        assistant's overview answer.
        """
        normalized_roles = {str(item).strip().lower() for item in roles if str(item).strip()}
        normalized_groups = {str(item).strip().lower() for item in groups if str(item).strip()}
        acl_matches = [and_(
            DocumentAclRecord.principal_type == "user",
            DocumentAclRecord.principal_id == subject_id.lower(),
        )]
        if normalized_roles:
            acl_matches.append(and_(
                DocumentAclRecord.principal_type == "role",
                DocumentAclRecord.principal_id.in_(normalized_roles),
            ))
        if normalized_groups:
            acl_matches.append(and_(
                DocumentAclRecord.principal_type == "group",
                DocumentAclRecord.principal_id.in_(normalized_groups),
            ))
        allowed = or_(
            DocumentRecord.access_scope == "public",
            select(DocumentAclRecord.id).where(
                DocumentAclRecord.document_id == DocumentRecord.id,
                or_(*acl_matches),
            ).exists(),
        )
        if not allow_confidential:
            allowed = and_(allowed, DocumentRecord.classification.in_(sorted(OPEN_CLASSIFICATIONS)))
        document_statement = (
            select(DocumentRecord, DocumentVersionRecord)
            .join(DocumentVersionRecord, DocumentVersionRecord.document_id == DocumentRecord.id)
            .where(DocumentVersionRecord.status == "indexed", allowed)
            .order_by(DocumentRecord.title, DocumentRecord.id)
            .limit(max(1, min(int(max_documents), 100)))
        )
        with self._sessions() as session:
            documents = session.execute(document_statement).all()
            version_ids = [version.id for _, version in documents]
            chunks = session.execute(
                select(DocumentChunkRecord)
                .where(DocumentChunkRecord.document_version_id.in_(version_ids))
                .order_by(DocumentChunkRecord.document_version_id, DocumentChunkRecord.ordinal)
            ).scalars().all() if version_ids else []
        chunks_by_version: dict[int, list[DocumentChunkRecord]] = {}
        for chunk in chunks:
            chunks_by_version.setdefault(chunk.document_version_id, []).append(chunk)
        result = []
        section_limit = max(1, min(int(max_sections), 50))
        for document, version in documents:
            headings, first_chunks = [], {}
            for chunk in chunks_by_version.get(version.id, []):
                heading = (chunk.heading or "正文").strip()
                if heading not in first_chunks:
                    headings.append(heading)
                    first_chunks[heading] = chunk
                if len(headings) >= section_limit:
                    break
            first = first_chunks.get(headings[0]) if headings else None
            result.append({
                "title": document.title,
                "version": version.version,
                "sections": headings,
                "chunk": (first.ordinal + 1) if first else None,
                "section": headings[0] if headings else "",
                "page": first.page_number if first else None,
            })
        return result

    def hybrid_search(self, query: str, embedding: list[float], limit: int = 5, *,
                      subject_id: str = "legacy", roles=(), groups=(),
                      allow_confidential: bool = False) -> list[dict]:
        if self.backend == "postgresql":
            return self._postgres_hybrid_search(
                query, embedding, limit, subject_id, roles, groups, allow_confidential,
            )
        return self._portable_hybrid_search(
            query, embedding, limit, subject_id, roles, groups, allow_confidential,
        )

    def lexical_search(self, query: str, limit: int = 5, *, subject_id: str = "legacy",
                       roles=(), groups=(), allow_confidential: bool = False) -> list[dict]:
        if self.backend == "postgresql":
            statement = text("""
                SELECT c.id, d.title, d.source_key, v.version, c.ordinal, c.heading, c.page_number,
                       c.content, ts_rank_cd(
                           c.search_vector, websearch_to_tsquery('simple', :query)
                       ) AS score
                FROM document_chunks c
                JOIN document_versions v ON v.id = c.document_version_id
                JOIN documents d ON d.id = v.document_id
                WHERE v.status = 'indexed'
                  AND (d.access_scope = 'public' OR EXISTS (
                    SELECT 1 FROM document_acl a
                    WHERE a.document_id = d.id AND (
                      (a.principal_type = 'user' AND a.principal_id = :subject_id)
                      OR (a.principal_type = 'role' AND a.principal_id = ANY(string_to_array(:acl_roles, ',')))
                      OR (a.principal_type = 'group' AND a.principal_id = ANY(string_to_array(:acl_groups, ',')))
                    )
                  ))
                  AND (d.classification = ANY(string_to_array(:open_classifications, ','))
                       OR :allow_confidential)
                  AND c.search_vector @@ websearch_to_tsquery('simple', :query)
                ORDER BY score DESC LIMIT :result_limit
            """)
            with self.engine.connect() as connection:
                return [dict(row) for row in connection.execute(statement, {
                    "query": self._postgres_websearch_query(query), "result_limit": limit,
                    "subject_id": subject_id, "acl_roles": ",".join(roles),
                    "acl_groups": ",".join(groups),
                    "open_classifications": ",".join(sorted(OPEN_CLASSIFICATIONS)),
                    "allow_confidential": allow_confidential,
                }).mappings().all()]
        results = self._portable_hybrid_search(
            query, [0.0] * 1024, max(limit * 4, 20), subject_id, roles, groups,
            allow_confidential,
        )
        lexical = [item for item in results if item.get("lexical_rank") is not None]
        return sorted(lexical, key=lambda item: item["lexical_rank"])[:limit]

    def _postgres_hybrid_search(self, query: str, embedding: list[float], limit: int,
                                subject_id: str, roles, groups,
                                allow_confidential: bool = False) -> list[dict]:
        statement = text("""
            WITH eligible AS (
                SELECT c.*, d.title, d.source_key, v.version
                FROM document_chunks c
                JOIN document_versions v ON v.id = c.document_version_id
                JOIN documents d ON d.id = v.document_id
                WHERE v.status = 'indexed'
                  AND (d.access_scope = 'public' OR EXISTS (
                    SELECT 1 FROM document_acl a
                    WHERE a.document_id = d.id AND (
                      (a.principal_type = 'user' AND a.principal_id = :subject_id)
                      OR (a.principal_type = 'role' AND a.principal_id = ANY(string_to_array(:acl_roles, ',')))
                      OR (a.principal_type = 'group' AND a.principal_id = ANY(string_to_array(:acl_groups, ',')))
                    )
                  ))
                  AND (d.classification = ANY(string_to_array(:open_classifications, ','))
                       OR :allow_confidential)
            ), lexical AS (
                SELECT id, row_number() OVER (ORDER BY lexical_score DESC) AS lexical_rank
                FROM (
                    SELECT id, ts_rank_cd(
                        search_vector, websearch_to_tsquery('simple', :query)
                    ) lexical_score
                    FROM eligible
                    WHERE search_vector @@ websearch_to_tsquery('simple', :query)
                    ORDER BY lexical_score DESC LIMIT :candidate_limit
                ) ranked
            ), semantic AS (
                SELECT id, similarity,
                       row_number() OVER (ORDER BY similarity DESC) AS semantic_rank
                FROM (
                    SELECT id, 1 - (embedding <=> CAST(:embedding AS vector)) similarity
                    FROM eligible
                    ORDER BY embedding <=> CAST(:embedding AS vector)
                    LIMIT :candidate_limit
                ) ranked
            ), candidates AS (
                SELECT id FROM lexical UNION SELECT id FROM semantic
            )
            SELECT e.id, e.title, e.source_key, e.version, e.ordinal, e.heading, e.page_number,
                   e.content,
                   l.lexical_rank, s.semantic_rank, s.similarity,
                   COALESCE(1.0 / (60 + l.lexical_rank), 0) +
                   COALESCE(1.0 / (60 + s.semantic_rank), 0) AS score
            FROM candidates x
            JOIN eligible e ON e.id = x.id
            LEFT JOIN lexical l ON l.id = x.id
            LEFT JOIN semantic s ON s.id = x.id
            WHERE l.lexical_rank IS NOT NULL OR s.similarity >= :semantic_threshold
            ORDER BY score DESC LIMIT :result_limit
        """)
        vector_value = "[" + ",".join(f"{value:.10f}" for value in embedding) + "]"
        with self.engine.connect() as connection:
            rows = connection.execute(statement, {
                "query": self._postgres_websearch_query(query),
                "embedding": vector_value,
                "candidate_limit": max(20, limit * 5),
                "semantic_threshold": 0.35,
                "result_limit": limit,
                "subject_id": subject_id, "acl_roles": ",".join(roles),
                "acl_groups": ",".join(groups),
                "open_classifications": ",".join(sorted(OPEN_CLASSIFICATIONS)),
                "allow_confidential": allow_confidential,
            }).mappings().all()
        return [dict(row) for row in rows]

    def _portable_hybrid_search(self, query: str, embedding: list[float], limit: int,
                                subject_id: str = "legacy", roles=(), groups=(),
                                allow_confidential: bool = False) -> list[dict]:
        statement = (
            select(DocumentChunkRecord, DocumentRecord, DocumentVersionRecord.version)
            .join(DocumentVersionRecord, DocumentVersionRecord.id == DocumentChunkRecord.document_version_id)
            .join(DocumentRecord, DocumentRecord.id == DocumentVersionRecord.document_id)
            .where(DocumentVersionRecord.status == "indexed")
        )
        with self._sessions() as session:
            rows = session.execute(statement).all()
            document_ids = {document.id for _, document, _ in rows}
            acl_rows = session.execute(
                select(
                    DocumentAclRecord.document_id,
                    DocumentAclRecord.principal_type,
                    DocumentAclRecord.principal_id,
                ).where(DocumentAclRecord.document_id.in_(document_ids))
            ).all() if document_ids else []
        acl_by_document: dict[int, set[tuple[str, str]]] = {}
        for document_id, principal_type, principal_id in acl_rows:
            acl_by_document.setdefault(document_id, set()).add((principal_type, principal_id))
        roles = {str(item).lower() for item in roles}
        groups = {str(item).lower() for item in groups}
        rows = [row for row in rows if self._document_allowed(
            row[1], subject_id, roles, groups, acl_by_document.get(row[1].id, set()),
            allow_confidential,
        )]
        if not rows:
            return []
        query_terms = lexical_terms(query)
        term_counts = [Counter((chunk.search_text or "").split()) for chunk, _, _ in rows]
        average_length = sum(sum(counts.values()) for counts in term_counts) / len(term_counts)
        document_frequency = Counter(
            term for counts in term_counts for term in set(counts) if term in query_terms
        )
        lexical_scores: dict[int, float] = {}
        semantic_scores: dict[int, float] = {}
        for (chunk, _, _), counts in zip(rows, term_counts):
            length = sum(counts.values()) or 1
            score = 0.0
            for term in query_terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                inverse = math.log(1 + (len(rows) - document_frequency[term] + 0.5) /
                                   (document_frequency[term] + 0.5))
                score += inverse * frequency / (
                    frequency + 1.2 * (0.25 + 0.75 * length / (average_length or 1))
                )
            lexical_scores[chunk.id] = score
            semantic_scores[chunk.id] = self._cosine(embedding, chunk.embedding)
        lexical_rank = {
            chunk_id: rank for rank, (chunk_id, score) in enumerate(
                sorted(lexical_scores.items(), key=lambda item: item[1], reverse=True), 1,
            ) if score > 0
        }
        semantic_rank = {
            chunk_id: rank for rank, (chunk_id, _score) in enumerate(
                sorted(semantic_scores.items(), key=lambda item: item[1], reverse=True), 1,
            )
        }
        results = []
        for chunk, document, version in rows:
            similarity = semantic_scores[chunk.id]
            if chunk.id not in lexical_rank and similarity < 0.35:
                continue
            score = (
                (1 / (60 + lexical_rank[chunk.id])) if chunk.id in lexical_rank else 0
            ) + (1 / (60 + semantic_rank[chunk.id]))
            results.append({
                "id": chunk.id, "title": document.title, "source_key": document.source_key,
                "version": version,
                "ordinal": chunk.ordinal, "heading": chunk.heading,
                "page_number": chunk.page_number, "content": chunk.content,
                "lexical_rank": lexical_rank.get(chunk.id),
                "semantic_rank": semantic_rank[chunk.id],
                "similarity": similarity, "score": score,
            })
        return sorted(results, key=lambda item: item["score"], reverse=True)[:limit]

    @staticmethod
    def _document_allowed(document, subject_id: str, roles, groups, entries,
                          allow_confidential: bool = False) -> bool:
        # Classification narrows access before the ACL is even consulted, so a document marked
        # public but classified confidential still stays out of reach of a low-clearance caller.
        if not allow_confidential and not is_open_classification(document.classification):
            return False
        if document.access_scope == "public":
            return True
        return (
            ("user", subject_id.lower()) in entries
            or any(("role", role) in entries for role in roles)
            or any(("group", group) in entries for group in groups)
        )

    def set_document_acl(self, document_id: int, entries, *, actor_subject_id: str,
                         request_id: str, access_scope: str = "restricted") -> None:
        allowed_types = {"user", "group", "role"}
        access_scope = str(access_scope).strip().lower()
        if access_scope not in {"public", "restricted"}:
            raise ValueError("文档访问范围无效")
        normalized = {(str(kind).lower(), str(value).strip().lower()) for kind, value in entries}
        if any(
            kind not in allowed_types or not value or len(value) > 256
            for kind, value in normalized
        ):
            raise ValueError("文档 ACL 主体格式无效")
        if access_scope == "public":
            normalized = set()
        with self._sessions.begin() as session:
            document = session.get(DocumentRecord, document_id)
            if document is None:
                raise ValueError("文档不存在")
            document.access_scope = access_scope
            document.updated_at = datetime.now(timezone.utc)
            session.query(DocumentAclRecord).filter(
                DocumentAclRecord.document_id == document_id,
            ).delete()
            session.add_all(DocumentAclRecord(
                document_id=document_id, principal_type=kind, principal_id=value,
                created_by_subject_id=actor_subject_id[:64],
            ) for kind, value in sorted(normalized))
            session.add(AuditEventRecord(
                actor_subject_id=actor_subject_id[:64], action="document_acl_replace",
                target_type="document", target_ref=str(document_id), result="success",
                request_id=request_id[:128],
            ))

    def document_acl(self, document_id: int) -> list[dict]:
        statement = select(DocumentAclRecord).where(
            DocumentAclRecord.document_id == document_id,
        ).order_by(DocumentAclRecord.id)
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [{"principal_type": row.principal_type, "principal_id": row.principal_id}
                for row in rows]

    def document_access(self, document_id: int) -> dict:
        with self._sessions() as session:
            document = session.get(DocumentRecord, document_id)
        if document is None:
            raise ValueError("文档不存在")
        return {
            "document_id": document.id,
            "access_scope": document.access_scope,
            "classification": document.classification,
            "entries": self.document_acl(document_id),
        }

    def record_audit_event(self, *, actor_subject_id: str, action: str,
                           target_type: str, target_ref: str, result: str,
                           request_id: str) -> None:
        with self._sessions.begin() as session:
            session.add(AuditEventRecord(
                actor_subject_id=actor_subject_id[:64],
                action=action[:64],
                target_type=target_type[:32],
                target_ref=target_ref[:512],
                result=result[:16],
                request_id=request_id[:128],
            ))

    def runtime_model_config(self) -> dict | None:
        try:
            with self._sessions() as session:
                row = session.get(RuntimeModelConfigRecord, 1)
        except (OSError, SQLAlchemyError):
            return None
        if row is None:
            return None
        return {
            "mode": row.mode,
            "provider": row.provider,
            "model": row.model,
            "response_strategy": getattr(row, "response_strategy", "knowledge_first") or "knowledge_first",
            "updated_by_subject_id": row.updated_by_subject_id,
            "updated_at": row.updated_at.isoformat(),
        }

    def set_runtime_model_config(self, *, mode: str, provider: str, model: str,
                                 response_strategy: str = "knowledge_first",
                                 actor_subject_id: str, request_id: str) -> dict:
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            row = session.get(RuntimeModelConfigRecord, 1)
            if row is None:
                row = RuntimeModelConfigRecord(id=1)
                session.add(row)
            row.mode = mode[:16]
            row.provider = provider[:32]
            row.model = model[:128]
            row.response_strategy = response_strategy[:32]
            row.updated_by_subject_id = actor_subject_id[:64]
            row.updated_at = now
            session.add(AuditEventRecord(
                actor_subject_id=actor_subject_id[:64], action="model_config_update",
                target_type="model_config", target_ref=f"{mode}:{provider}:{model}"[:512],
                result="success", request_id=request_id[:128],
            ))
        return self.runtime_model_config() or {}

    def runtime_provider_credentials(self) -> dict[str, str]:
        try:
            with self._sessions() as session:
                rows = session.scalars(select(RuntimeProviderCredentialRecord)).all()
        except (OSError, SQLAlchemyError):
            return {}
        values = {}
        for row in rows:
            try:
                values[row.provider] = self._credential_cipher.decrypt(
                    row.ciphertext.encode("ascii")
                ).decode("utf-8")
            except (InvalidToken, ValueError, UnicodeError):
                continue
        return values

    def set_runtime_provider_credential(self, *, provider: str, api_key: str,
                                        actor_subject_id: str, request_id: str) -> None:
        ciphertext = self._credential_cipher.encrypt(api_key.encode("utf-8")).decode("ascii")
        with self._sessions.begin() as session:
            row = session.get(RuntimeProviderCredentialRecord, provider[:32])
            if row is None:
                row = RuntimeProviderCredentialRecord(provider=provider[:32])
                session.add(row)
            row.ciphertext = ciphertext
            row.updated_by_subject_id = actor_subject_id[:64]
            row.updated_at = datetime.now(timezone.utc)
            session.add(AuditEventRecord(
                actor_subject_id=actor_subject_id[:64], action="provider_credential_update",
                target_type="provider", target_ref=provider[:32], result="success",
                request_id=request_id[:128],
            ))

    def audit_events(self, limit: int = 100) -> list[dict]:
        count = max(1, min(int(limit), 200))
        statement = select(AuditEventRecord).order_by(
            AuditEventRecord.id.desc(),
        ).limit(count)
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [self._audit_event_public(row) for row in rows]

    @staticmethod
    def _audit_event_public(row: AuditEventRecord) -> dict:
        return {
            "id": row.id,
            "actor_subject_id": row.actor_subject_id,
            "action": row.action,
            "target_type": row.target_type,
            "target_ref": row.target_ref,
            "result": row.result,
            "request_id": row.request_id,
            "created_at": row.created_at.isoformat(),
        }

    # Upper bound on a single export so a misconfigured client cannot dump the whole table at once.
    AUDIT_EXPORT_MAX_ROWS = 10000

    def audit_events_export(self, *, start=None, end=None, action=None, target_type=None,
                            actor=None, limit: int = 1000) -> list[dict]:
        """Chronological audit export honouring the same capability gate as ``audit_events``.

        `start`/`end` are inclusive bounds on `created_at`; string filters narrow by exact match.
        Rows are capped at ``AUDIT_EXPORT_MAX_ROWS`` and ordered oldest-first for an audit trail.
        """
        count = max(1, min(int(limit), self.AUDIT_EXPORT_MAX_ROWS))
        statement = select(AuditEventRecord)
        if start is not None:
            statement = statement.where(AuditEventRecord.created_at >= self._normalize_dt(start))
        if end is not None:
            statement = statement.where(AuditEventRecord.created_at <= self._normalize_dt(end))
        if action:
            statement = statement.where(AuditEventRecord.action == action[:64])
        if target_type:
            statement = statement.where(AuditEventRecord.target_type == target_type[:32])
        if actor:
            statement = statement.where(AuditEventRecord.actor_subject_id == actor[:64])
        statement = statement.order_by(AuditEventRecord.id.asc()).limit(count)
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [self._audit_event_public(row) for row in rows]

    @staticmethod
    def _normalize_dt(value):
        """Coerce a bound datetime to UTC so SQLite (naive string) and Postgres (timestamptz) compare consistently."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _cosine(left, right) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        numerator = sum(float(a) * float(b) for a, b in zip(left, right))
        left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
        right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return max(-1.0, min(1.0, numerator / (left_norm * right_norm)))

    @staticmethod
    def _postgres_websearch_query(query: str) -> str:
        return " OR ".join(f'"{term}"' for term in lexical_terms(query))

    def usage_ledger(self, session_id: str, limit: int = 50,
                     owner_subject_id: str = "legacy") -> list[dict]:
        count = max(1, min(int(limit), 100))
        statement = (
            select(ModelUsageRecord)
            .join(QueryRecord, QueryRecord.id == ModelUsageRecord.query_id)
            .where(QueryRecord.session_id == str(session_id or "default")[:128])
            .where(QueryRecord.owner_subject_id == owner_subject_id[:64])
            .order_by(ModelUsageRecord.id.desc())
            .limit(count)
        )
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [self._usage_public(row) for row in rows]

    def usage_summary(self, session_id: str, owner_subject_id: str = "legacy") -> dict:
        statement = (
            select(
                func.count(ModelUsageRecord.id),
                func.sum(case((ModelUsageRecord.status == "succeeded", 1), else_=0)),
                func.coalesce(func.sum(ModelUsageRecord.prompt_tokens), 0),
                func.coalesce(func.sum(ModelUsageRecord.completion_tokens), 0),
                func.coalesce(func.sum(ModelUsageRecord.total_tokens), 0),
                func.coalesce(func.sum(ModelUsageRecord.cost_cny), 0),
                func.sum(case((
                    (ModelUsageRecord.status == "succeeded")
                    & ModelUsageRecord.usage_reported
                    & (ModelUsageRecord.cost_cny.is_(None)), 1
                ), else_=0)),
                func.sum(case((
                    (ModelUsageRecord.status == "succeeded")
                    & (ModelUsageRecord.usage_reported.is_(False)), 1
                ), else_=0)),
            )
            .join(QueryRecord, QueryRecord.id == ModelUsageRecord.query_id)
            .where(QueryRecord.session_id == str(session_id or "default")[:128])
            .where(QueryRecord.owner_subject_id == owner_subject_id[:64])
        )
        with self._sessions() as session:
            row = session.execute(statement).one()
        return {
            "attempts": int(row[0] or 0),
            "successful_calls": int(row[1] or 0),
            "prompt_tokens": int(row[2] or 0),
            "completion_tokens": int(row[3] or 0),
            "total_tokens": int(row[4] or 0),
            "cost_cny": float(row[5] or 0),
            "unpriced_calls": int(row[6] or 0),
            "unmetered_calls": int(row[7] or 0),
            "currency": "CNY",
        }

    @staticmethod
    def _usage_public(row: ModelUsageRecord) -> dict:
        return {
            "id": row.id,
            "query_id": row.query_id,
            "document_version_id": row.document_version_id,
            "operation": row.operation,
            "request_id": row.request_id,
            "provider": row.provider,
            "model": row.model,
            "route": row.route,
            "attempt": row.attempt,
            "status": row.status,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "total_tokens": row.total_tokens,
            "usage_reported": row.usage_reported,
            "cost_cny": float(row.cost_cny) if row.cost_cny is not None else None,
            "input_price_cny_per_million": (
                float(row.input_price_cny) if row.input_price_cny is not None else None
            ),
            "output_price_cny_per_million": (
                float(row.output_price_cny) if row.output_price_cny is not None else None
            ),
            "provider_request_id": row.provider_request_id,
            "latency_ms": row.latency_ms,
            "error_code": row.error_code,
            "created_at": row.created_at.isoformat(),
        }
