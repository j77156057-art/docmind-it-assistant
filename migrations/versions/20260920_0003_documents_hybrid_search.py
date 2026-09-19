"""Add versioned documents, chunks, pgvector and generalized model usage.

Revision ID: 20260920_0003
Revises: 20260920_0002
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector


revision: str = "20260920_0003"
down_revision: Union[str, Sequence[str], None] = "20260920_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_key", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_key"),
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "content_sha256", name="uq_document_versions_hash"),
        sa.UniqueConstraint("document_id", "version", name="uq_document_versions_number"),
    )
    embedding_type = Vector(1024) if dialect == "postgresql" else sa.JSON()
    chunk_columns = [
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_version_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading", sa.String(length=512), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("embedding", embedding_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]
    if dialect == "postgresql":
        chunk_columns.append(sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', search_text)", persisted=True),
            nullable=False,
        ))
    op.create_table(
        "document_chunks",
        *chunk_columns,
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_version_id", "ordinal", name="uq_document_chunks_ordinal"),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.create_index("ix_document_versions_status", "document_versions", ["status"])
    op.create_index("ix_document_chunks_document_version_id", "document_chunks", ["document_version_id"])
    if dialect == "postgresql":
        op.create_index(
            "ix_document_chunks_search_vector", "document_chunks", ["search_vector"],
            postgresql_using="gin",
        )
        op.create_index(
            "ix_document_chunks_embedding_hnsw", "document_chunks", ["embedding"],
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        )

    if dialect == "sqlite":
        with op.batch_alter_table("model_usage_ledger") as batch:
            batch.alter_column("query_id", existing_type=sa.Integer(), nullable=True)
            batch.add_column(sa.Column("document_version_id", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("operation", sa.String(length=32), server_default="chat", nullable=False))
            batch.create_foreign_key(
                "fk_model_usage_document_version", "document_versions",
                ["document_version_id"], ["id"], ondelete="CASCADE",
            )
            batch.create_index("ix_model_usage_ledger_document_version_id", ["document_version_id"])
    else:
        op.alter_column("model_usage_ledger", "query_id", existing_type=sa.Integer(), nullable=True)
        op.add_column("model_usage_ledger", sa.Column("document_version_id", sa.Integer(), nullable=True))
        op.add_column(
            "model_usage_ledger",
            sa.Column("operation", sa.String(length=32), server_default="chat", nullable=False),
        )
        op.create_foreign_key(
            "fk_model_usage_document_version", "model_usage_ledger", "document_versions",
            ["document_version_id"], ["id"], ondelete="CASCADE",
        )
        op.create_index(
            "ix_model_usage_ledger_document_version_id", "model_usage_ledger",
            ["document_version_id"],
        )


def downgrade() -> None:
    dialect = op.get_context().dialect.name
    op.execute("DELETE FROM model_usage_ledger WHERE query_id IS NULL")
    if dialect == "sqlite":
        with op.batch_alter_table("model_usage_ledger") as batch:
            batch.drop_index("ix_model_usage_ledger_document_version_id")
            batch.drop_constraint("fk_model_usage_document_version", type_="foreignkey")
            batch.drop_column("operation")
            batch.drop_column("document_version_id")
            batch.alter_column("query_id", existing_type=sa.Integer(), nullable=False)
    else:
        op.drop_index("ix_model_usage_ledger_document_version_id", table_name="model_usage_ledger")
        op.drop_constraint(
            "fk_model_usage_document_version", "model_usage_ledger", type_="foreignkey",
        )
        op.drop_column("model_usage_ledger", "operation")
        op.drop_column("model_usage_ledger", "document_version_id")
        op.alter_column("model_usage_ledger", "query_id", existing_type=sa.Integer(), nullable=False)
        op.drop_index("ix_document_chunks_embedding_hnsw", table_name="document_chunks")
        op.drop_index("ix_document_chunks_search_vector", table_name="document_chunks")
    op.drop_index("ix_document_chunks_document_version_id", table_name="document_chunks")
    op.drop_index("ix_document_versions_status", table_name="document_versions")
    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_table("document_chunks")
    op.drop_table("document_versions")
    op.drop_table("documents")
