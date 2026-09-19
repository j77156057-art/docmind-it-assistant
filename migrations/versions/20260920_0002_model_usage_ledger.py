"""Add immutable model token and cost ledger.

Revision ID: 20260920_0002
Revises: 20260920_0001
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260920_0002"
down_revision: Union[str, Sequence[str], None] = "20260920_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def create_ledger_table() -> None:
    op.create_table(
        "model_usage_ledger",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("query_id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("route", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("usage_reported", sa.Boolean(), nullable=False),
        sa.Column("input_price_cny", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("output_price_cny", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("cost_cny", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("provider_request_id", sa.String(length=128), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["query_id"], ["queries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def upgrade() -> None:
    if context.is_offline_mode():
        create_ledger_table()
    elif not sa.inspect(op.get_bind()).has_table("model_usage_ledger"):
        create_ledger_table()
    index_names = (
        set() if context.is_offline_mode()
        else {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("model_usage_ledger")}
    )
    if "ix_model_usage_ledger_query_id" not in index_names:
        op.create_index("ix_model_usage_ledger_query_id", "model_usage_ledger", ["query_id"])
    if "ix_model_usage_ledger_request_id" not in index_names:
        op.create_index("ix_model_usage_ledger_request_id", "model_usage_ledger", ["request_id"])
    if "ix_model_usage_ledger_created_at" not in index_names:
        op.create_index("ix_model_usage_ledger_created_at", "model_usage_ledger", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_model_usage_ledger_created_at", table_name="model_usage_ledger")
    op.drop_index("ix_model_usage_ledger_request_id", table_name="model_usage_ledger")
    op.drop_index("ix_model_usage_ledger_query_id", table_name="model_usage_ledger")
    op.drop_table("model_usage_ledger")
