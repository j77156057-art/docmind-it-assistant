"""Add the ingestion job queue used by the asynchronous indexing worker.

Revision ID: 20260921_0009
Revises: 20260921_0008

Design notes
------------
* One row per ``(job_type, version_id)``. Re-importing, retrying a failed job, or reopening a
  rejected version re-queues that same row rather than stacking duplicates, so the queue cannot
  be flooded by repeated submissions of one version.
* ``attempts``/``max_attempts`` live on the row so a stale-job reclaim can decide the outcome
  without reading application configuration.
* ``heartbeat_at`` exists so a crashed worker's job can be reclaimed instead of staying
  ``running`` forever.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260921_0009"
down_revision: Union[str, Sequence[str], None] = "20260921_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Frozen snapshot of the vocabulary introduced by this revision.
JOB_TYPES = ("import", "reindex", "withdraw", "evaluate")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
TYPE_CHECK = "job_type IN (" + ", ".join(f"'{item}'" for item in JOB_TYPES) + ")"
STATUS_CHECK = "status IN (" + ", ".join(f"'{item}'" for item in JOB_STATUSES) + ")"

INDEXES = (
    ("ix_ingestion_jobs_job_type", ["job_type"]),
    ("ix_ingestion_jobs_status", ["status"]),
    ("ix_ingestion_jobs_document_id", ["document_id"]),
    ("ix_ingestion_jobs_version_id", ["version_id"]),
    ("ix_ingestion_jobs_claim", ["status", "priority", "id"]),
    ("ix_ingestion_jobs_created_at", ["created_at"]),
)


def upgrade() -> None:
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_subject_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(TYPE_CHECK, name="ck_ingestion_jobs_type"),
        sa.CheckConstraint(STATUS_CHECK, name="ck_ingestion_jobs_status"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_type", "version_id", name="uq_ingestion_jobs_target"),
    )
    for name, columns in INDEXES:
        op.create_index(name, "ingestion_jobs", columns)


def downgrade() -> None:
    # Dropping this table discards queue history (attempts, error codes, who asked for the job).
    # Back up first if the queue is used as an operational record.
    for name, _columns in reversed(INDEXES):
        op.drop_index(name, table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
