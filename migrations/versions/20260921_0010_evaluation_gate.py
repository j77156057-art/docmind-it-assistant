"""Add the golden-question evaluation gate.

Revision ID: 20260921_0010
Revises: 20260921_0009

Design notes
------------
* ``evaluation_cases`` is test data owned by the knowledge team; it never stores user content.
* ``evaluation_runs.gate_result`` is nullable on purpose: ``NULL`` means "the gate was switched
  off", which must stay distinguishable from "evaluated and passed".
* ``evaluation_case_results.detail`` holds counters and ranks only, never answer text, so the
  evaluation tables cannot become a second copy of the Q&A content the privacy rules exclude.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260921_0010"
down_revision: Union[str, Sequence[str], None] = "20260921_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Frozen snapshots of the vocabulary introduced by this revision.
EVAL_TRIGGERS = ("manual", "pre_publish", "scheduled")
EVAL_STATUSES = ("running", "succeeded", "failed")
GATE_MODES = ("off", "warn", "block")
GATE_RESULTS = ("pass", "warn", "block", "overridden")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "evaluation_cases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("case_key", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expect_refusal", sa.Boolean(), nullable=False),
        sa.Column("expected_document_key", sa.String(length=512), nullable=True),
        sa.Column("expected_heading", sa.String(length=512), nullable=True),
        sa.Column("tags", sa.String(length=256), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_key", name="uq_evaluation_cases_key"),
    )

    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("document_version_id", sa.Integer(), nullable=True),
        sa.Column("gate_mode", sa.String(length=8), nullable=False),
        sa.Column("gate_result", sa.String(length=12), nullable=True),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("passed_cases", sa.Integer(), nullable=False),
        sa.Column("failed_cases", sa.Integer(), nullable=False),
        sa.Column("recall_at_k", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("citation_accuracy", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("refusal_accuracy", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("baseline_run_id", sa.Integer(), nullable=True),
        sa.Column("gate_reason", sa.String(length=512), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(_in_clause("trigger", EVAL_TRIGGERS), name="ck_evaluation_runs_trigger"),
        sa.CheckConstraint(_in_clause("status", EVAL_STATUSES), name="ck_evaluation_runs_status"),
        sa.CheckConstraint(_in_clause("gate_mode", GATE_MODES), name="ck_evaluation_runs_gate_mode"),
        sa.CheckConstraint(
            "gate_result IS NULL OR " + _in_clause("gate_result", GATE_RESULTS),
            name="ck_evaluation_runs_gate_result",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["baseline_run_id"], ["evaluation_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evaluation_runs_trigger", "evaluation_runs", ["trigger"])
    op.create_index("ix_evaluation_runs_status", "evaluation_runs", ["status"])
    op.create_index(
        "ix_evaluation_runs_version_id", "evaluation_runs", ["document_version_id", "id"],
    )

    op.create_table(
        "evaluation_case_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("retrieved", sa.Boolean(), nullable=False),
        sa.Column("matched_rank", sa.Integer(), nullable=True),
        sa.Column("citation_ok", sa.Boolean(), nullable=True),
        sa.Column("refusal_ok", sa.Boolean(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["evaluation_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["case_id"], ["evaluation_cases.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "case_id", name="uq_evaluation_case_results_case"),
    )
    op.create_index("ix_evaluation_case_results_run_id", "evaluation_case_results", ["run_id"])
    op.create_index("ix_evaluation_case_results_case_id", "evaluation_case_results", ["case_id"])


def downgrade() -> None:
    # Dropping these tables discards the quality history that justifies past publishes.
    # Back up first: the gate result of an already-published version lives only here.
    op.drop_index("ix_evaluation_case_results_case_id", table_name="evaluation_case_results")
    op.drop_index("ix_evaluation_case_results_run_id", table_name="evaluation_case_results")
    op.drop_table("evaluation_case_results")
    op.drop_index("ix_evaluation_runs_version_id", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_status", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_trigger", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_table("evaluation_cases")
