"""Add identity ownership, document ACL, and security audit events.

Revision ID: 20260920_0004
Revises: 20260920_0003
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260920_0004"
down_revision: Union[str, Sequence[str], None] = "20260920_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("queries") as batch:
        batch.add_column(sa.Column(
            "owner_subject_id", sa.String(length=64), server_default="legacy", nullable=False,
        ))
        batch.create_index("ix_queries_owner_subject_id", ["owner_subject_id"])
    with op.batch_alter_table("documents") as batch:
        # Existing indexed knowledge remains available to authenticated viewers.
        batch.add_column(sa.Column(
            "access_scope", sa.String(length=16), server_default="public", nullable=False,
        ))
        batch.add_column(sa.Column(
            "classification", sa.String(length=32), server_default="internal", nullable=False,
        ))

    op.create_table(
        "document_acl",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("principal_type", sa.String(length=16), nullable=False),
        sa.Column("principal_id", sa.String(length=256), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "principal_type IN ('user', 'group', 'role')",
            name="ck_document_acl_principal_type",
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "principal_type", "principal_id", name="uq_document_acl_principal",
        ),
    )
    op.create_index("ix_document_acl_document_id", "document_acl", ["document_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor_subject_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_ref", sa.String(length=512), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_actor_subject_id", "audit_events", ["actor_subject_id"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_request_id", table_name="audit_events")
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_subject_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_document_acl_document_id", table_name="document_acl")
    op.drop_table("document_acl")
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("classification")
        batch.drop_column("access_scope")
    with op.batch_alter_table("queries") as batch:
        batch.drop_index("ix_queries_owner_subject_id")
        batch.drop_column("owner_subject_id")
