"""采购硬缺口信号（引用 / 反馈 / 知识缺口 / 组织模型）。

Revision ID: 20260922_0013
Revises: 20260922_0012

设计要点（见 docs/architecture-procurement-gaps.md §3）
---------------------------------------------------
* `query_citations` / `query_feedback` 随 `queries` 级联清理（ON DELETE CASCADE，PostgreSQL 生效；
  SQLite 遵循项目既有"显式清理"约定）。
* `knowledge_gaps.query_id` 为弱引用（ON DELETE SET NULL），源 query 删除后缺口仍可读。
* `users.subject_id` / `groups.group_key` 与既有 `document_acl.principal_id` 同构，零迁移。
* `departments.parent_key` 自引用使用 `use_alter=True`，允许孤儿（SET NULL）。
* 缺口表 `gap_summary` 仅为非 PII 运营模板，不复制加密的 `queries.question`。
* PostgreSQL 由 CI 校验；本地 SQLite 用 `alembic upgrade head` 生成全表。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260922_0013"
down_revision: Union[str, Sequence[str], None] = "20260922_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "query_citations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("query_id", sa.Integer(), nullable=False),
        sa.Column("citation_kind", sa.String(length=16), nullable=False),
        sa.Column("document_chunk_id", sa.Integer(), nullable=True),
        sa.Column("document_version_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=512), nullable=False),
        sa.Column("section", sa.String(length=512), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("line_number", sa.Integer(), nullable=True),
        sa.Column("chunk_ordinal", sa.Integer(), nullable=True),
        sa.Column("score", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("citation_rank", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "citation_kind IN ('chunk', 'knowledge')", name="ck_query_citations_kind",
        ),
        sa.ForeignKeyConstraint(["query_id"], ["queries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_chunk_id"], ["document_chunks.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_query_citations_query_id", "query_citations", ["query_id"])

    op.create_table(
        "query_feedback",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("query_id", sa.Integer(), nullable=False),
        sa.Column("actor_subject_id", sa.String(length=64), nullable=False),
        sa.Column("rating", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "rating IN ('positive', 'negative')", name="ck_query_feedback_rating",
        ),
        sa.ForeignKeyConstraint(["query_id"], ["queries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "query_id", "actor_subject_id", name="uq_query_feedback_query_actor",
        ),
    )
    op.create_index("ix_query_feedback_query_id", "query_feedback", ["query_id"])
    op.create_index(
        "ix_query_feedback_actor_subject_id", "query_feedback", ["actor_subject_id"],
    )

    op.create_table(
        "knowledge_gaps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("query_id", sa.Integer(), nullable=True),
        sa.Column("gap_type", sa.String(length=32), nullable=False),
        sa.Column("gap_summary", sa.String(length=1000), nullable=False),
        sa.Column("model_route", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolved_version_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "gap_type IN ('insufficient_evidence')", name="ck_knowledge_gaps_type",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'addressed', 'dismissed')", name="ck_knowledge_gaps_status",
        ),
        sa.ForeignKeyConstraint(["query_id"], ["queries.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["resolved_version_id"], ["document_versions.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_gaps_query_id", "knowledge_gaps", ["query_id"])

    op.create_table(
        "users",
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_users_status"),
        sa.PrimaryKeyConstraint("subject_id"),
    )

    op.create_table(
        "groups",
        sa.Column("group_key", sa.String(length=256), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("group_key"),
    )

    op.create_table(
        "user_group_memberships",
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("group_key", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["users.subject_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_key"], ["groups.group_key"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("subject_id", "group_key"),
    )

    op.create_table(
        "departments",
        sa.Column("department_key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("parent_key", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_key"], ["departments.department_key"],
            ondelete="SET NULL", use_alter=True,
        ),
        sa.PrimaryKeyConstraint("department_key"),
    )

    op.create_table(
        "user_department",
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("department_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["users.subject_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["department_key"], ["departments.department_key"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("subject_id", "department_key"),
    )


def downgrade() -> None:
    op.drop_table("user_department")
    op.drop_table("departments")
    op.drop_table("user_group_memberships")
    op.drop_table("groups")
    op.drop_table("users")
    op.drop_index("ix_knowledge_gaps_query_id", table_name="knowledge_gaps")
    op.drop_table("knowledge_gaps")
    op.drop_index("ix_query_feedback_actor_subject_id", table_name="query_feedback")
    op.drop_index("ix_query_feedback_query_id", table_name="query_feedback")
    op.drop_table("query_feedback")
    op.drop_index("ix_query_citations_query_id", table_name="query_citations")
    op.drop_table("query_citations")
