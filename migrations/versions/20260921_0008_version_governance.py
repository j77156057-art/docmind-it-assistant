"""Add knowledge-governance version states, governance pointers, and the review trail.

Revision ID: 20260921_0008
Revises: 20260920_0007

Design notes
------------
* ``indexed`` keeps its original meaning ("published and retrievable"), so no retrieval SQL
  changes here. Everything new sits *before* it (``queued``/``processing``/``staged``) or
  *after* it (``rejected``/``withdrawn``).
* ``pending`` is renamed to ``queued`` and existing rows are backfilled **before** the CHECK
  constraint is added, otherwise the constraint would reject the pre-existing vocabulary.
* Downgrade never maps an unreviewed state back to ``indexed``. The old code treats ``indexed``
  as retrievable, so doing that would publish drafts. See ``LEGACY_DOWNGRADE_STATUS`` below.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260921_0008"
down_revision: Union[str, Sequence[str], None] = "20260920_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Frozen snapshots of the vocabulary introduced by this revision. Deliberately NOT imported from
# backend.db_models: a migration must keep describing the schema of its own point in time.
GOVERNANCE_STATUSES = (
    "queued", "processing", "staged", "indexed",
    "rejected", "withdrawn", "superseded", "failed",
)
REVIEW_ACTIONS = (
    "submit", "reopen", "approve", "reject", "publish", "withdraw", "rollback", "override_gate",
)
STATUS_CHECK = "status IN (" + ", ".join(f"'{item}'" for item in GOVERNANCE_STATUSES) + ")"
ACTION_CHECK = "action IN (" + ", ".join(f"'{item}'" for item in REVIEW_ACTIONS) + ")"

BACKFILL_ACTOR = "system:backfill"
BACKFILL_REQUEST_ID = f"migration-{revision}"

# Downgrade mapping for states the pre-0008 code cannot represent.
# `staged` MUST NOT become `indexed`: unreviewed content would instantly become retrievable.
LEGACY_DOWNGRADE_STATUS = {
    "queued": "pending",
    "processing": "pending",
    "staged": "pending",
    "rejected": "failed",
    "withdrawn": "superseded",
}

NEW_COLUMNS = (
    sa.Column("submitted_by_subject_id", sa.String(length=64), nullable=True),
    sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("published_by_subject_id", sa.String(length=64), nullable=True),
    sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("withdrawn_reason", sa.String(length=512), nullable=True),
    sa.Column("superseded_by_version_id", sa.Integer(), nullable=True),
)
NEW_COLUMN_NAMES = (
    "submitted_by_subject_id", "submitted_at", "indexed_at", "published_by_subject_id",
    "withdrawn_at", "withdrawn_reason", "superseded_by_version_id",
)


def upgrade() -> None:
    dialect = op.get_context().dialect.name

    # 1) Normalize the legacy vocabulary first so the CHECK constraint can be added.
    op.execute("UPDATE document_versions SET status = 'queued' WHERE status = 'pending'")

    # 2) Governance pointers (columns only; the CHECK constraint needs the backfill below).
    #    The self-referential link to the replacing version is created through the same batch so
    #    PostgreSQL and SQLite end up with the same schema (alembic check must stay clean).
    with op.batch_alter_table("document_versions") as batch:
        for column in NEW_COLUMNS:
            batch.add_column(column)
        batch.create_foreign_key(
            "fk_document_versions_superseded_by_version", "document_versions",
            ["superseded_by_version_id"], ["id"], ondelete="SET NULL",
        )

    # 3) Backfill the new pointer for versions that were already live.
    op.execute(
        "UPDATE document_versions SET indexed_at = COALESCE(published_at, created_at) "
        "WHERE status = 'indexed'"
    )

    # 4) Constrain the vocabulary and index the review queue.
    with op.batch_alter_table("document_versions") as batch:
        batch.create_check_constraint("ck_document_versions_status", STATUS_CHECK)
        batch.create_index(
            "ix_document_versions_status_submitted", ["status", "submitted_at"],
        )

    # 5) Append-only approval trail.
    op.create_table(
        "document_version_reviews",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_version_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("from_status", sa.String(length=24), nullable=False),
        sa.Column("to_status", sa.String(length=24), nullable=False),
        sa.Column("actor_subject_id", sa.String(length=64), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("is_override", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(ACTION_CHECK, name="ck_document_version_reviews_action"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_document_version_reviews_document_version_id",
        "document_version_reviews", ["document_version_id"],
    )
    op.create_index(
        "ix_document_version_reviews_action", "document_version_reviews", ["action"],
    )
    op.create_index(
        "ix_document_version_reviews_actor_subject_id",
        "document_version_reviews", ["actor_subject_id"],
    )
    op.create_index(
        "ix_document_version_reviews_request_id", "document_version_reviews", ["request_id"],
    )
    op.create_index(
        "ix_document_version_reviews_created_at", "document_version_reviews", ["created_at"],
    )

    # 5) Backfill a synthetic publish decision for versions that were already live before this
    #    revision, so no retrievable document exists without an explained approval record.
    op.execute(sa.text(
        "INSERT INTO document_version_reviews ("
        "document_version_id, action, from_status, to_status, actor_subject_id, comment,"
        " is_override, request_id, created_at"
        ") SELECT id, 'publish', 'indexed', 'indexed', :actor, :comment, false, :request_id,"
        " COALESCE(published_at, created_at) FROM document_versions WHERE status = 'indexed'"
    ).bindparams(
        actor=BACKFILL_ACTOR,
        comment="历史数据回填：该版本在本迁移之前已发布，未经审批流程",
        request_id=BACKFILL_REQUEST_ID,
    ))


def downgrade() -> None:
    dialect = op.get_context().dialect.name

    # Back up first: dropping document_version_reviews destroys the approval evidence chain.
    op.drop_index(
        "ix_document_version_reviews_created_at", table_name="document_version_reviews",
    )
    op.drop_index(
        "ix_document_version_reviews_request_id", table_name="document_version_reviews",
    )
    op.drop_index(
        "ix_document_version_reviews_actor_subject_id", table_name="document_version_reviews",
    )
    op.drop_index("ix_document_version_reviews_action", table_name="document_version_reviews")
    op.drop_index(
        "ix_document_version_reviews_document_version_id", table_name="document_version_reviews",
    )
    op.drop_table("document_version_reviews")

    if dialect != "sqlite":
        op.drop_constraint(
            "fk_document_versions_superseded_by_version", "document_versions", type_="foreignkey",
        )

    # The CHECK constraint must go before the legacy vocabulary is restored, otherwise it rejects
    # the very values the pre-0008 code expects.
    with op.batch_alter_table("document_versions") as batch:
        batch.drop_index("ix_document_versions_status_submitted")
        batch.drop_constraint("ck_document_versions_status", type_="check")

    for status, legacy in LEGACY_DOWNGRADE_STATUS.items():
        op.execute(sa.text(
            "UPDATE document_versions SET status = :legacy WHERE status = :current"
        ).bindparams(legacy=legacy, current=status))

    with op.batch_alter_table("document_versions") as batch:
        for name in reversed(NEW_COLUMN_NAMES):
            batch.drop_column(name)
