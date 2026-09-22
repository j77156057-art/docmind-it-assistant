"""P2 ACL 切换：document_acl CHECK 扩展含 department；users 加 oidc_sub 列。

Revision ID: 20260922_0015
Revises: 20260922_0014

设计要点（见 docs/architecture-p2-acl-switch.md §6）
---------------------------------------------------
* document_acl.principal_type CHECK 由 IN ('user','group','role')
  扩展为 IN ('user','group','role','department')（应用层 + 迁移双写约束）。
* users 新增可空 oidc_sub 列，存**应用内 subject_id（HMAC 派生的哈希值，与 Principal.subject_id 同值）**，而非原始 OIDC sub；原始 sub 仅登录时用于派生 subject_id，不落库、不进 Principal.repr（隐私要求，见 test_oidc_validates_signature_claims_and_normalizes_identity）。
* departments / user_department 已在 0013 建好，不重建。
* 双向可回滚：downgrade 反向操作（CHECK 回退为三型 + 删 oidc_sub 列）。
* 使用 op.batch_alter_table：SQLite 重建表保全数据，PG 就地 ALTER（与母体 0004 一致）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260922_0015"
down_revision: Union[str, Sequence[str], None] = "20260922_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Extend the document_acl principal_type CHECK and add users.oidc_sub."""
    with op.batch_alter_table("document_acl") as batch_op:
        batch_op.drop_constraint("ck_document_acl_principal_type", type_="check")
        batch_op.create_check_constraint(
            "ck_document_acl_principal_type",
            "principal_type IN ('user', 'group', 'role', 'department')",
        )
    op.add_column(
        "users",
        sa.Column("oidc_sub", sa.String(length=256), nullable=True),
    )


def downgrade() -> None:
    """Reverse the migration: drop oidc_sub and revert the CHECK to the three original types."""
    op.drop_column("users", "oidc_sub")
    with op.batch_alter_table("document_acl") as batch_op:
        batch_op.drop_constraint("ck_document_acl_principal_type", type_="check")
        batch_op.create_check_constraint(
            "ck_document_acl_principal_type",
            "principal_type IN ('user', 'group', 'role')",
        )
