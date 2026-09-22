"""Add faithfulness metrics to evaluation runs.

Revision ID: 20260922_0017
Revises: 20260922_0016
"""

from collections import OrderedDict

from alembic import op
import sqlalchemy as sa


revision = "20260922_0017"
down_revision = "20260922_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.add_column(sa.Column("faithfulness", sa.Numeric(6, 4), nullable=True))
        batch_op.add_column(
            sa.Column("faithfulness_coverage", sa.Numeric(6, 4), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.drop_column("faithfulness_coverage")
        batch_op.drop_column("faithfulness")
