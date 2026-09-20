"""Store encrypted runtime provider credentials.

Revision ID: 20260920_0007
Revises: 20260920_0006
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260920_0007"
down_revision: Union[str, Sequence[str], None] = "20260920_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runtime_provider_credentials",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("updated_by_subject_id", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider"),
    )


def downgrade() -> None:
    op.drop_table("runtime_provider_credentials")
