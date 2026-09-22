"""Field-level encryption for query.question (Phase 3, A-4)."""
import tempfile
from pathlib import Path

from backend.config import AppSettings
from backend.crypto import FieldEncryptor, PREFIX
from backend.database import QueryDatabase
from backend.db_models import QueryRecord


def _raw_question(database: QueryDatabase, query_id: int) -> str:
    with database._sessions() as session:
        return session.get(QueryRecord, query_id).question


class TestFieldEncryptor:
    def test_round_trip(self):
        enc = FieldEncryptor("a-real-key-material-string")
        secret = "如何配置 VPN 与审计日志？"
        token = enc.encrypt(secret)
        assert token != secret
        assert token.startswith(PREFIX)
        assert enc.decrypt(token) == secret

    def test_empty_value_passthrough(self):
        enc = FieldEncryptor("k")
        assert enc.encrypt("") == ""
        assert enc.decrypt("") == ""

    def test_legacy_plaintext_passthrough(self):
        enc = FieldEncryptor("k")
        # A row written before encryption existed has no marker prefix.
        assert enc.decrypt("plain old question") == "plain old question"

    def test_wrong_key_does_not_reveal_plaintext(self):
        a = FieldEncryptor("key-a")
        b = FieldEncryptor("key-b")
        token = a.encrypt("top secret query")
        # Decrypt with the wrong key returns the stored (marked) value, not plaintext.
        assert b.decrypt(token) != "top secret query"
        assert b.decrypt(token).startswith(PREFIX)

    def test_default_key_is_development_key(self):
        enc = FieldEncryptor(None)
        secret = "dev mode query"
        assert enc.decrypt(enc.encrypt(secret)) == secret


class TestQueryQuestionEncryption:
    def test_record_stores_ciphertext_history_returns_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = QueryDatabase(
                f"sqlite:///{(Path(tmp) / 'q.db').as_posix()}",
                query_field_key="test-field-key",
            )
            qid = db.record("sess", "VPN 配置步骤是什么？", "sufficient", "knowledge")
            stored = _raw_question(db, qid)
            assert stored.startswith(PREFIX)
            assert stored != "VPN 配置步骤是什么？"
            items = db.history("sess")
            assert items[0]["question"] == "VPN 配置步骤是什么？"

    def test_legacy_plaintext_row_kept_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = QueryDatabase(
                f"sqlite:///{(Path(tmp) / 'q.db').as_posix()}",
                query_field_key="test-field-key",
            )
            db.initialize()
            with db._sessions.begin() as session:
                session.add(QueryRecord(
                    session_id="sess", owner_subject_id="legacy",
                    question="明文旧记录", evidence="e", model_route="knowledge",
                ))
            items = db.history("sess")
            # No marker prefix -> returned verbatim (backwards compatible).
            assert items[0]["question"] == "明文旧记录"


class TestConfigRequiresKeyInProduction:
    def test_production_without_key_raises(self):
        import pytest
        with pytest.raises(ValueError):
            AppSettings(
                environment="production",
                auth_mode="oidc",
                oidc_issuer="https://idp.example.com",
                oidc_audience="aud",
                oidc_jwks_url="https://idp.example.com/jwks",
                oidc_client_id="cid",
                oidc_redirect_uri="https://app.example.com/callback",
                database_url="postgresql://localhost/db",
                embedding_mode="provider",
                auth_subject_salt="x" * 32,
                query_field_key="",
            )

    def test_production_with_key_ok(self):
        settings = AppSettings(
            environment="production",
            auth_mode="oidc",
            oidc_issuer="https://idp.example.com",
            oidc_audience="aud",
            oidc_jwks_url="https://idp.example.com/jwks",
            oidc_client_id="cid",
            oidc_redirect_uri="https://app.example.com/callback",
            database_url="postgresql://localhost/db",
            embedding_mode="provider",
            auth_subject_salt="x" * 32,
            query_field_key="a-strong-field-key",
        )
        assert settings.query_field_key.get_secret_value() == "a-strong-field-key"
