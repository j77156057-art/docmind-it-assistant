"""Field-level encryption for sensitive query text (``QueryRecord.question``).

Uses Fernet (AES-128-CBC with HMAC-SHA256). The application key material comes
from ``IT_QUERY_FIELD_KEY``. A raw 32-byte Fernet key is accepted verbatim; any
other string is derived into a 32-byte key via SHA-256 so operators can supply a
passphrase instead of a pre-encoded Fernet token.

Encrypted values are stored with an ``enc:v1:`` marker prefix so reads can tell
ciphertext from legacy plaintext unambiguously, which keeps rows written before
encryption was enabled readable after the feature is turned on.
"""
from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

LOGGER = logging.getLogger(__name__)

PREFIX = "enc:v1:"
_DEV_KEY_MATERIAL = "docmind-development-field-key-do-not-use-in-prod"


def _derive_key(key_material: str) -> bytes:
    """Return a 32-byte url-safe Fernet key derived from arbitrary key material."""
    return base64.urlsafe_b64encode(hashlib.sha256(key_material.encode("utf-8")).digest())


class FieldEncryptor:
    """Encrypt/decrypt a single string field with Fernet.

    When no key material is supplied a fixed development key is used and a warning
    is emitted; production deployments must configure ``IT_QUERY_FIELD_KEY`` (the
    configuration layer enforces this). Legacy plaintext values (no marker prefix)
    are returned verbatim on decrypt so rows written before encryption was enabled
    keep working without a migration.
    """

    def __init__(self, key_material: str | None = None) -> None:
        material = (key_material or "").strip()
        if not material:
            material = _DEV_KEY_MATERIAL
            LOGGER.warning(
                "IT_QUERY_FIELD_KEY 未设置，使用开发期弱密钥加密 query.question；"
                "生产环境必须配置 IT_QUERY_FIELD_KEY。"
            )
        self._fernet = Fernet(_derive_key(material))

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return plaintext
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
        return f"{PREFIX}{token}"

    def decrypt(self, stored: str) -> str:
        if not stored or not stored.startswith(PREFIX):
            return stored  # legacy plaintext row written before field encryption
        token = stored[len(PREFIX):]
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            LOGGER.error("无法解密 query.question：密钥不匹配或数据损坏（%s）", exc)
            return stored
