"""知识域归属（decision #6，最小形态）：新增 knowledge_domains + documents.domain_key。

Revision ID: 20260923_0020
Revises: 20260923_0019

设计要点
--------
* 引入「知识域」概念，作为文档的单域归属元数据容器。本期检索与 ACL 不做域级隔离，
  域只承载归属信息，供后续版本按域过滤（见 decision #6）。
* knowledge_domains.domain_key 为主键（String(64)）。documents.domain_key 为可空外键，
  引用 knowledge_domains.domain_key，删除域时 SET NULL：存量文档（domain_key 为 NULL）
  不受影响，删除一个域只会把引用它的文档置空，而非级联删除文档。
* documents.domain_key 可空：对存量行无破坏性（NULL 表示未归属任何域）。索引显式以
  ix_documents_domain_key 命名，避免 batch_alter_table 在 SQLite 重建表时产生临时索引名。
* 双向可回滚：downgrade 先删除索引与外键、再删除列（batch_alter_table 兼容 SQLite 重建表），
  最后删除 knowledge_domains 表；与 0015 / 0016 / 0019 的批处理风格一致。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260923_0020"
down_revision: Union[str, Sequence[str], None] = "20260923_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create knowledge_domains and add the nullable documents.domain_key FK + index."""
    op.create_table(
        "knowledge_domains",
        sa.Column("domain_key", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(sa.Column("domain_key", sa.String(64), nullable=True))
        batch_op.create_foreign_key(
            "fk_documents_domain_key", "knowledge_domains",
            ["domain_key"], ["domain_key"], ondelete="SET NULL",
        )
    op.create_index("ix_documents_domain_key", "documents", ["domain_key"])


def downgrade() -> None:
    """Drop index + FK + column, then the knowledge_domains table."""
    op.drop_index("ix_documents_domain_key", table_name="documents")
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_constraint("fk_documents_domain_key", type_="foreignkey")
        batch_op.drop_column("domain_key")
    op.drop_table("knowledge_domains")
