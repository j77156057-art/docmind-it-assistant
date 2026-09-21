"""Persist ingestion-job retry backoff on the row.

Revision ID: 20260922_0011
Revises: 20260921_0010

Design notes
------------
* ``next_attempt_at`` lets a retryable failure schedule its own next attempt (exponential backoff,
  capped) instead of relying only on the worker's in-process sleep. The claim query skips jobs
  whose ``next_attempt_at`` is in the future, so a broken provider is not hammered in a tight loop.
* The claim index is widened to include ``next_attempt_at`` so the "due now" filter stays indexed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260922_0011"
down_revision: Union[str, Sequence[str], None] = "20260921_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_ingestion_jobs_claim2",
        "ingestion_jobs",
        ["status", "next_attempt_at", "priority", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_claim2", table_name="ingestion_jobs")
    op.drop_column("ingestion_jobs", "next_attempt_at")
