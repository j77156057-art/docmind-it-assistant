"""Add the singleton runtime model configuration.

Revision ID: 20260920_0005
Revises: 20260920_0004
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260920_0005"
down_revision: Union[str, Sequence[str], None] = "20260920_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runtime_model_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("updated_by_subject_id", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_runtime_model_config_singleton"),
        sa.CheckConstraint(
            "mode IN ('knowledge', 'local', 'cloud')", name="ck_runtime_model_config_mode",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("runtime_model_config")
