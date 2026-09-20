"""Add persisted answer-generation strategy.

Revision ID: 20260920_0006
Revises: 20260920_0005
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260920_0006"
down_revision: Union[str, Sequence[str], None] = "20260920_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "runtime_model_config",
        sa.Column("response_strategy", sa.String(length=32), nullable=False,
                  server_default="knowledge_first"),
    )


def downgrade() -> None:
    op.drop_column("runtime_model_config", "response_strategy")
