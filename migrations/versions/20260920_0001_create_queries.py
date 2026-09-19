"""Create the isolated IT query history schema.

Revision ID: 20260920_0001
Revises:
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260920_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def create_queries_table() -> None:
    op.create_table(
        "queries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("evidence", sa.String(length=32), nullable=False),
        sa.Column("model_route", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def upgrade() -> None:
    if context.is_offline_mode():
        create_queries_table()
        op.create_index("ix_queries_session_id", "queries", ["session_id"], unique=False)
        op.create_index("ix_queries_created_at", "queries", ["created_at"], unique=False)
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("queries"):
        create_queries_table()
    index_names = {item["name"] for item in sa.inspect(bind).get_indexes("queries")}
    if "ix_queries_session_id" not in index_names:
        op.create_index("ix_queries_session_id", "queries", ["session_id"], unique=False)
    if "ix_queries_created_at" not in index_names:
        op.create_index("ix_queries_created_at", "queries", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_queries_created_at", table_name="queries")
    op.drop_index("ix_queries_session_id", table_name="queries")
    op.drop_table("queries")
