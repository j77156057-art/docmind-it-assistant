"""Persist the strict (first-chunk) citation accuracy alongside the relaxed one.

The dual-track citation judgment (architect review 2026-09-23) reports both
``citation_accuracy`` (any expected-document chunk whose heading matches — the gate's
effective signal) and ``citation_accuracy_strict`` (only the *first* matched chunk's heading
matches — the historical behavior). The strict aggregate was computed but never stored, so a
run record could not show how many cases were "recalled but mis-ranked" versus "never recalled".
This migration adds the column so both numbers survive in the evaluation_runs record.

Revision ID: 20260923_0018
Revises: 20260922_0017
"""

from collections import OrderedDict

from alembic import op
import sqlalchemy as sa


revision = "20260923_0018"
down_revision = "20260922_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.add_column(
            sa.Column("citation_accuracy_strict", sa.Numeric(6, 4), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.drop_column("citation_accuracy_strict")
