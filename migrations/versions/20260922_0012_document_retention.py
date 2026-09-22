"""Document retention soft-mark column.

Revision ID: 20260922_0012
Revises: 20260922_0011

Design notes
------------
* `documents.expired_at` is NULL while a document is inside its retention window. When the window
  elapses the retention job stamps it (soft mark); after the grace window the row is hard-deleted
  together with every child row (versions, chunks, ACL, jobs, reviews, usage, evaluation runs).
* Soft-then-hard keeps the record recoverable for the grace window (合规留痕) while still reclaiming
  storage (存储回收). The purge deletes children explicitly so it is correct on SQLite without
  relying on ON DELETE CASCADE (which SQLite only honours when PRAGMA foreign_keys=ON).
* Default window is 365 days retention + 30 days grace, both overridable via IT_RETENTION_DAYS /
  IT_RETENTION_GRACE_DAYS.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260922_0012"
down_revision: Union[str, Sequence[str], None] = "20260922_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "expired_at")
