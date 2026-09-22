"""document_chunks.content 字段级加密（复用 IT_QUERY_FIELD_KEY + FieldEncryptor）。

Revision ID: 20260922_0014
Revises: 20260922_0013

设计要点（见 docs/architecture-body-redaction.md §4）
---------------------------------------------------
* 仅就地回填 document_chunks.content；不新增表/列，healthcheck 无需变更。
* 加密复用 backend.crypto.FieldEncryptor 与 IT_QUERY_FIELD_KEY（缺失走开发弱密钥，与运行时一致）。
* 分批（每批 1000）`WHERE content NOT LIKE 'enc:v1:%'` 保证幂等、可重复运行。
* 空值跳过；旧明文（无前缀）在读取端由 decrypt 原样返回，向后兼容。
* downgrade 保留密文（生产不回滚）；解密需与加密相同的密钥，密钥轮换后旧行无法解密。
"""
import os
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

from backend.crypto import FieldEncryptor, PREFIX


revision: str = "20260922_0014"
down_revision: Union[str, Sequence[str], None] = "20260922_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Already-encrypted values carry the ``enc:v1:`` marker; the trailing ``%`` makes the
# LIKE expression match "starts with the marker", so encrypted rows are excluded from re-encryption.
_LIKE_PATTERN = f"{PREFIX}%"
_BATCH_SIZE = 1000


def upgrade() -> None:
    """Encrypt every stored ``document_chunks.content`` value in idempotent batches."""
    encryptor = FieldEncryptor(os.environ.get("IT_QUERY_FIELD_KEY") or None)
    connection = op.get_bind()
    while True:
        rows = connection.execute(
            text(
                "SELECT id, content FROM document_chunks "
                "WHERE content NOT LIKE :marker LIMIT :batch"
            ),
            {"marker": _LIKE_PATTERN, "batch": _BATCH_SIZE},
        ).fetchall()
        if not rows:
            break
        for row_id, content in rows:
            if content is None or content.startswith(PREFIX):
                continue
            connection.execute(
                text("UPDATE document_chunks SET content = :enc WHERE id = :id"),
                {"enc": encryptor.encrypt(content), "id": row_id},
            )
        if len(rows) < _BATCH_SIZE:
            break


def downgrade() -> None:
    # 生产不回滚：解密需要与原加密相同的 IT_QUERY_FIELD_KEY，若密钥曾轮换则旧行无法解密；
    # 用错密钥会把密文就地破坏。保留密文（enc:v1: 前缀）是更保守的选择。读取端 decrypt 对
    # 无前缀明文原样返回，因此未加密的旧库仍可正常读取，本迁移无需清理任何内容。
    # 如确需在本地回退到明文，请用与加密时完全一致的密钥另跑一次性脚本解密存量行。
    pass
