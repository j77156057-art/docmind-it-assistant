"""决策 #1：单枚 IT_AUTH_SUBJECT_SALT 拆分为三枚独立密钥的回归测试。

覆盖：
* 会话 JWT 使用独立 IT_AUTH_SESSION_SECRET 签名；设置后不能与仅 subject_salt 互相验签。
* IT_AUTH_SESSION_SECRET 留空时回退到 subject_salt（向后兼容，存量会话仍可验证）。
* 凭证 Fernet 使用独立 IT_PROVIDER_CREDENTIAL_KEY；设置后旧 salt 派生的密文仍可经 legacy 路径读出。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from backend.auth import OIDCAuthenticator, subject_identifier
from backend.database import QueryDatabase


def _authenticator(subject_salt: str, session_secret: str) -> OIDCAuthenticator:
    return OIDCAuthenticator(
        mode="development", issuer="", audience="", jwks_url="", subject_salt=subject_salt, session_secret=session_secret,
    )


def test_session_secret_split_is_independent():
    # Token signed with a distinct session_secret must NOT verify under only the subject salt.
    signed = _authenticator("subject-salt-0123456789", "session-secret-0123456789").issue_session(
        subject="u", roles=("viewer",), groups=(), display_name="U", hours=1,
    )
    fallback_auth = _authenticator("subject-salt-0123456789", "")
    raised = False
    try:
        fallback_auth._authenticate_session_token(signed)
    except Exception:
        raised = True
    assert raised, "session token signed with IT_AUTH_SESSION_SECRET must not verify under subject_salt alone"


def test_session_secret_fallback_to_subject_salt():
    # When session_secret unset, it falls back to subject_salt, so a token still verifies.
    auth = _authenticator("subject-salt-0123456789", "")
    token = auth.issue_session(subject="u", roles=("viewer",), groups=(), display_name="U", hours=1)
    principal = auth._authenticate_session_token(token)
    assert principal.subject_id == subject_identifier("u", "subject-salt-0123456789")


def test_credential_legacy_decrypt_after_key_split():
    with tempfile.TemporaryDirectory() as root:
        db_old = QueryDatabase(str(Path(root) / "creds.db"), credential_key="old-salt-key")
        db_old.initialize()
        db_old.set_runtime_provider_credential(
            provider="qwen", api_key="legacy-secret",
            actor_subject_id="admin", request_id="split-test",
        )
        # New instance: current key 'new', legacy key 'old' (the previously-used salt-derived key).
        db_new = QueryDatabase(
            str(Path(root) / "creds.db"),
            credential_key="new-salt-key", legacy_credential_key="old-salt-key",
        )
        creds = db_new.runtime_provider_credentials()
        assert creds.get("qwen") == "legacy-secret"
