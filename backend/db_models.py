"""SQLAlchemy persistence models shared by the repository and Alembic."""
from __future__ import annotations

from datetime import datetime, timezone

from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator
from pgvector.sqlalchemy import Vector


EMBEDDING_DIMENSION = 1024

# Knowledge-governance vocabulary. Declared once and reused by the CHECK constraints below so a
# new status cannot be added to the application without also constraining the database.
# `indexed` keeps its original meaning: published and retrievable.
VERSION_STATUSES = (
    "queued",       # accepted, waiting for an indexing worker
    "processing",   # parsing, chunking, embedding
    "staged",       # indexed but NOT retrievable, waiting for review
    "indexed",      # published and retrievable
    "rejected",     # review declined
    "withdrawn",    # published, then taken offline
    "superseded",   # replaced by a newer published version
    "failed",       # processing failed
)
REVIEW_ACTIONS = (
    "submit", "reopen", "approve", "reject", "publish", "withdraw", "rollback", "override_gate",
)
# Ingestion queue vocabulary. Deliberately small: business states the administrator can see and
# act on, not the framework's internal execution states.
JOB_TYPES = ("import", "reindex", "withdraw", "evaluate")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
ACTIVE_JOB_STATUSES = ("queued", "running")

# Evaluation-gate vocabulary. A run is either a manual quality check or the gate that guards a
# publish. `gate_result` is NULL when the gate is switched off, so "not evaluated" is never
# confused with "evaluated and passed".
EVAL_TRIGGERS = ("manual", "pre_publish", "scheduled")
EVAL_STATUSES = ("running", "succeeded", "failed")
GATE_MODES = ("off", "warn", "block")
GATE_RESULTS = ("pass", "warn", "block", "overridden")
# Statuses that may never be returned by any retrieval path.
UNPUBLISHED_STATUSES = tuple(
    status for status in VERSION_STATUSES if status != "indexed"
)
# Statuses that mean "a version with this content is already handled"; re-importing the same
# content is a no-op for these.
IN_FLIGHT_STATUSES = ("queued", "processing", "staged")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"



class EmbeddingVector(TypeDecorator):
    """Use pgvector in PostgreSQL and JSON vectors in isolated SQLite tests."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Vector(EMBEDDING_DIMENSION))
        return dialect.type_descriptor(JSON())


class Base(DeclarativeBase):
    pass


class QueryRecord(Base):
    __tablename__ = "queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    owner_subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(String(32), nullable=False)
    model_route: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True,
    )


class ModelUsageRecord(Base):
    __tablename__ = "model_usage_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_id: Mapped[int | None] = mapped_column(
        ForeignKey("queries.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    document_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False, default="chat")
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    route: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_reported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_price_cny: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    output_price_cny: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    cost_cny: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True,
    )


class DocumentRecord(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    access_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="restricted")
    classification: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )


class DocumentVersionRecord(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_document_versions_number"),
        UniqueConstraint("document_id", "content_sha256", name="uq_document_versions_hash"),
        CheckConstraint(_in_clause("status", VERSION_STATUSES), name="ck_document_versions_status"),
        Index("ix_document_versions_status_submitted", "status", "submitted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Governance pointers. The approval chain itself lives in document_version_reviews.
    submitted_by_subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by_subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    superseded_by_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="SET NULL"), nullable=True,
    )


class DocumentVersionReviewRecord(Base):
    """Append-only knowledge-governance decision trail.

    Distinct from ``audit_events``: audit answers "who touched what", this answers
    "why is this version allowed to be live".
    """

    __tablename__ = "document_version_reviews"
    __table_args__ = (
        CheckConstraint(_in_clause("action", REVIEW_ACTIONS), name="ck_document_version_reviews_action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_version_id: Mapped[int] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    action: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    from_status: Mapped[str] = mapped_column(String(24), nullable=False)
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    comment: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    is_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True,
    )


class DocumentChunkRecord(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "ordinal", name="uq_document_chunks_ordinal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_version_id: Mapped[int] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(EmbeddingVector(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )


class DocumentAclRecord(Base):
    __tablename__ = "document_acl"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "principal_type", "principal_id", name="uq_document_acl_principal",
        ),
        CheckConstraint(
            "principal_type IN ('user', 'group', 'role')", name="ck_document_acl_principal_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    principal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(256), nullable=False)
    created_by_subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )


class RuntimeModelConfigRecord(Base):
    __tablename__ = "runtime_model_config"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_runtime_model_config_singleton"),
        CheckConstraint(
            "mode IN ('knowledge', 'local', 'cloud')", name="ck_runtime_model_config_mode",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    response_strategy: Mapped[str] = mapped_column(String(32), nullable=False, default="knowledge_first")
    updated_by_subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )


class RuntimeProviderCredentialRecord(Base):
    __tablename__ = "runtime_provider_credentials"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by_subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )


class EvaluationCaseRecord(Base):
    """One golden question. Test data, owned by the knowledge team — never user content."""

    __tablename__ = "evaluation_cases"
    __table_args__ = (
        UniqueConstraint("case_key", name="uq_evaluation_cases_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_key: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    expect_refusal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expected_document_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    expected_heading: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tags: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )


class EvaluationRunRecord(Base):
    """One execution of the golden set, optionally acting as the gate for a version."""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint(_in_clause("trigger", EVAL_TRIGGERS), name="ck_evaluation_runs_trigger"),
        CheckConstraint(_in_clause("status", EVAL_STATUSES), name="ck_evaluation_runs_status"),
        CheckConstraint(_in_clause("gate_mode", GATE_MODES), name="ck_evaluation_runs_gate_mode"),
        CheckConstraint(
            "gate_result IS NULL OR " + _in_clause("gate_result", GATE_RESULTS),
            name="ck_evaluation_runs_gate_result",
        ),
        Index("ix_evaluation_runs_version_id", "document_version_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    document_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=True,
    )
    gate_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    gate_result: Mapped[str | None] = mapped_column(String(12), nullable=True)
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recall_at_k: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    citation_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    refusal_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    baseline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="SET NULL"), nullable=True,
    )
    gate_reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_by_subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class EvaluationCaseResultRecord(Base):
    """Per-case outcome. ``detail`` holds counters and ranks only — never answer text."""

    __tablename__ = "evaluation_case_results"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id", name="uq_evaluation_case_results_case"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    case_id: Mapped[int] = mapped_column(
        ForeignKey("evaluation_cases.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    retrieved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    matched_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    citation_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    refusal_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class IngestionJobRecord(Base):
    """Business queue for document indexing work.

    The queue is deliberately owned by this application rather than by an orchestration
    framework: administrators must be able to see, retry and cancel jobs, and the audit trail
    must not depend on a framework's internal checkpoint format.
    """

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint(_in_clause("job_type", JOB_TYPES), name="ck_ingestion_jobs_type"),
        CheckConstraint(_in_clause("status", JOB_STATUSES), name="ck_ingestion_jobs_status"),
        # One job row per (type, version): a re-import or a rejected retry re-queues the same
        # row instead of stacking duplicates.
        UniqueConstraint("job_type", "version_id", name="uq_ingestion_jobs_target"),
        Index("ix_ingestion_jobs_claim", "status", "priority", "id"),
        Index("ix_ingestion_jobs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    version_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True,
    )
