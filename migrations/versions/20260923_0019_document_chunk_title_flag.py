"""父子块检索后续：document_chunks 新增 is_title_block 列。

Revision ID: 20260923_0019
Revises: 20260923_0018

设计要点
--------
* 检索排序需要区分「仅有标题、无正文」的块与「含正文的实质块」。分块阶段
  (ingestion/chunker.py) 已为前者打上 is_title_block=True，落库后供
  HybridRetriever 在 top-k 内降权、避免它们挤掉同标题的实质块（embedding-ab §4）。
* NOT NULL、数据库默认 FALSE：对存量行回填 FALSE 无破坏性；新索引的块按分块结果写入。
* 双向可回滚：downgrade 删除该列（与 0004 / 0015 / 0016 一致，使用 batch_alter_table
  以兼容 SQLite 重建表）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260923_0019"
down_revision: Union[str, Sequence[str], None] = "20260923_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the is_title_block column to document_chunks."""
    with op.batch_alter_table("document_chunks") as batch_op:
        batch_op.add_column(
            sa.Column("is_title_block", sa.Boolean(nullable=False, server_default=sa.false())),
        )


def downgrade() -> None:
    """Remove the is_title_block column from document_chunks."""
    with op.batch_alter_table("document_chunks") as batch_op:
        batch_op.drop_column("is_title_block")
