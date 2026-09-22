"""父子块检索：document_chunks 新增 parent_content 列。

Revision ID: 20260922_0016
Revises: 20260922_0015

设计要点
--------
* 为支持「小块检索 + 大块生成」的父子块策略，document_chunks 增加 parent_content
  列：存检索命中小块所属的大父块文本，供生成阶段回吐更完整上下文。
* 该列与 content 一样经字段级加密落库（见 QueryDatabase.persist_document_chunks）。
* NOT NULL、数据库默认空串：对存量行回填空串无破坏性；新索引的块会写入父块文本。
* 双向可回滚：downgrade 删除该列（与母体 0004 / 0015 一致，使用 batch_alter_table
  以兼容 SQLite 重建表）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260922_0016"
down_revision: Union[str, Sequence[str], None] = "20260922_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the parent_content column to document_chunks."""
    with op.batch_alter_table("document_chunks") as batch_op:
        batch_op.add_column(
            sa.Column("parent_content", sa.Text(), nullable=False, server_default=sa.text("''")),
        )


def downgrade() -> None:
    """Remove the parent_content column from document_chunks."""
    with op.batch_alter_table("document_chunks") as batch_op:
        batch_op.drop_column("parent_content")
