"""Field-level encryption for document_chunks.content (body redaction).

Mirrors tests/test_field_encryption.py but exercises the chunk body column:
the write paths (``persist_document_chunks`` / ``replace_document_chunk_batch``) encrypt
``content``, the read paths (``document_version_chunks`` / ``lexical_search`` /
``hybrid_search``) decrypt it, and the Alembic migration backfills legacy rows idempotently.

See docs/architecture-body-redaction.md.
"""
import os
import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from backend import QueryDatabase
from backend.crypto import FieldEncryptor, PREFIX
from backend.db_models import DocumentChunkRecord, DocumentVersionRecord
from ingestion.chunker import DocumentChunk


ROOT = Path(__file__).resolve().parents[1]


def _migration_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


def _db(url: str, key: str = "test-chunk-key") -> QueryDatabase:
    database = QueryDatabase(url, query_field_key=key)
    database.initialize()
    return database


def _seed_indexed_version(database: QueryDatabase, *, version_id: int = 1,
                          content: str = "zebra 是非洲草原上的动物",
                          embedding: list[float] | None = None) -> int:
    """Create a public/internal document + a queued version, write one chunk through the
    production write path (which now encrypts), then flip the version to indexed so the
    search/read paths return it. Returns the document id."""
    result = database.begin_document_import(
        source_key=f"manual/seed-{version_id}", title="Seed", mime_type="text/markdown",
        content_sha256=f"sha-{version_id}", access_scope="public", classification="internal",
        submitted_by_subject_id="legacy",
    )
    vid = result["version_id"]
    # The real indexing flow sets the version to "processing" before writing chunks.
    with database._sessions.begin() as session:
        session.get(DocumentVersionRecord, vid).status = "processing"
    database.persist_document_chunks(
        vid,
        [DocumentChunk(ordinal=0, heading="H", page_number=1, content=content)],
        [embedding if embedding is not None else [0.1, 0.2, 0.3]],
    )
    with database._sessions.begin() as session:
        session.get(DocumentVersionRecord, vid).status = "indexed"
    return int(result["document_id"])


class TestChunkFieldEncryptor(unittest.TestCase):
    def test_round_trip(self):
        enc = FieldEncryptor("chunk-key")
        secret = "如何配置 VPN 与审计日志？"
        token = enc.encrypt(secret)
        self.assertNotEqual(token, secret)
        self.assertTrue(token.startswith(PREFIX))
        self.assertEqual(enc.decrypt(token), secret)

    def test_empty_value_passthrough(self):
        enc = FieldEncryptor("k")
        self.assertEqual(enc.encrypt(""), "")
        self.assertEqual(enc.decrypt(""), "")

    def test_legacy_plaintext_passthrough(self):
        enc = FieldEncryptor("k")
        # A row written before encryption existed has no marker prefix.
        self.assertEqual(enc.decrypt("plain old body"), "plain old body")

    def test_wrong_key_does_not_reveal_plaintext(self):
        a = FieldEncryptor("key-a")
        b = FieldEncryptor("key-b")
        token = a.encrypt("top secret body")
        # Decrypt with the wrong key returns the stored (marked) value, not plaintext.
        self.assertNotEqual(b.decrypt(token), "top secret body")
        self.assertTrue(b.decrypt(token).startswith(PREFIX))

    def test_default_key_is_development_key(self):
        enc = FieldEncryptor(None)
        secret = "dev mode body"
        self.assertEqual(enc.decrypt(enc.encrypt(secret)), secret)


class TestChunkContentReadWrite(unittest.TestCase):
    def test_persist_stores_ciphertext_but_preview_reads_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{(Path(tmp) / 'c.db').as_posix()}"
            db = _db(url)
            doc_id = _seed_indexed_version(db, version_id=1, content="斑马条纹用于伪装")

            # Raw stored value is ciphertext.
            with db._sessions() as session:
                raw = session.get(DocumentChunkRecord, 1).content
            self.assertTrue(raw.startswith(PREFIX))
            self.assertNotEqual(raw, "斑马条纹用于伪装")

            # Reviewer preview decrypts back to plaintext.
            preview = db.document_version_chunks(doc_id, 1)
            self.assertEqual(preview["chunks"][0]["content"], "斑马条纹用于伪装")
            db.dispose()

    def test_persist_batch_stores_ciphertext(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{(Path(tmp) / 'c.db').as_posix()}"
            db = _db(url)
            result = db.begin_document_import(
                source_key="manual/batch", title="B", mime_type="text/markdown",
                content_sha256="sha-b", access_scope="public", classification="internal",
            )
            vid = result["version_id"]
            with db._sessions.begin() as session:
                session.get(DocumentVersionRecord, vid).status = "processing"
            db.replace_document_chunk_batch(vid, [{
                "ordinal": 0, "heading": "H", "page_number": 1,
                "content": "批量写入的明文", "embedding": [0.5, 0.5],
            }])
            with db._sessions() as session:
                raw = session.get(DocumentChunkRecord, 1).content
            self.assertTrue(raw.startswith(PREFIX))
            self.assertNotEqual(raw, "批量写入的明文")
            db.dispose()

    def test_lexical_search_returns_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{(Path(tmp) / 'c.db').as_posix()}"
            db = _db(url)
            _seed_indexed_version(db, version_id=2, content="zebra 是非洲草原上的动物")
            hits = db.lexical_search("zebra", limit=5, subject_id="legacy")
            self.assertTrue(hits)
            self.assertIn("zebra", hits[0]["content"])
            self.assertFalse(hits[0]["content"].startswith(PREFIX))
            db.dispose()

    def test_hybrid_search_returns_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{(Path(tmp) / 'c.db').as_posix()}"
            db = _db(url)
            emb = [0.1, 0.2, 0.3]
            _seed_indexed_version(db, version_id=3, content="语义检索返回明文", embedding=emb)
            hits = db.hybrid_search("无关查询", emb, subject_id="legacy")
            self.assertTrue(hits)
            self.assertEqual(hits[0]["content"], "语义检索返回明文")
            db.dispose()

    def test_legacy_plaintext_row_still_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{(Path(tmp) / 'c.db').as_posix()}"
            db = _db(url)
            result = db.begin_document_import(
                source_key="manual/legacy", title="L", mime_type="text/markdown",
                content_sha256="sha-l", access_scope="public", classification="internal",
            )
            vid = result["version_id"]
            with db._sessions.begin() as session:
                version = session.get(DocumentVersionRecord, vid)
                version.status = "processing"
                # Simulate a pre-encryption row: no marker prefix.
                session.add(DocumentChunkRecord(
                    document_version_id=vid, ordinal=0, heading="H", page_number=1,
                    content="旧明文未加密", search_text="旧明文未加密",
                    embedding=[0.0, 0.0],
                ))
                version.status = "indexed"
            preview = db.document_version_chunks(int(result["document_id"]), 1)
            self.assertEqual(preview["chunks"][0]["content"], "旧明文未加密")
            db.dispose()


class TestChunkMigrationBackfill(unittest.TestCase):
    """The migration encrypts legacy plaintext rows; it is idempotent and the downgrade is a
    no-op so production never rolls ciphertext back to plaintext."""

    _saved_key: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._saved_key = os.environ.get("IT_QUERY_FIELD_KEY")
        os.environ["IT_QUERY_FIELD_KEY"] = "migration-chunk-key"

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._saved_key is None:
            os.environ.pop("IT_QUERY_FIELD_KEY", None)
        else:
            os.environ["IT_QUERY_FIELD_KEY"] = cls._saved_key

    def test_upgrade_backfills_idempotently_downgrade_keeps_ciphertext(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{(Path(tmp) / 'mig.db').as_posix()}"
            config = _migration_config(url)

            # Apply everything up to (but not including) the new migration.
            command.upgrade(config, "20260922_0013")
            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO documents (id, source_key, title, mime_type, access_scope,"
                    " classification, created_at, updated_at) VALUES"
                    " (1, 'manual/v', 'V', 'text/markdown', 'public', 'internal',"
                    " '2026-01-01', '2026-01-01')"
                ))
                connection.execute(text(
                    "INSERT INTO document_versions (id, document_id, version, content_sha256,"
                    " status, chunk_count, created_at) VALUES"
                    " (1, 1, 1, 'h', 'indexed', 1, '2026-01-01')"
                ))
                connection.execute(text(
                    "INSERT INTO document_chunks (document_version_id, ordinal, heading,"
                    " page_number, content, search_text, embedding, created_at) VALUES"
                    " (1, 0, 'H', 1, '明文存量正文', '明文存量正文', '[0.1,0.2]', '2026-01-01')"
                ))
            engine.dispose()

            # Apply the new migration: the legacy plaintext row becomes ciphertext.
            command.upgrade(config, "head")
            engine = create_engine(url)
            with engine.connect() as connection:
                raw = connection.execute(text(
                    "SELECT content FROM document_chunks WHERE id = 1"
                )).scalar()
            self.assertTrue(raw.startswith(PREFIX))
            self.assertNotEqual(raw, "明文存量正文")

            # The same key reads it back as plaintext through the DB read path.
            db = QueryDatabase(url, query_field_key="migration-chunk-key")
            self.assertEqual(
                db.document_version_chunks(1, 1)["chunks"][0]["content"], "明文存量正文",
            )
            db.dispose()

            # Idempotency: downgrade (no-op, ciphertext kept) then upgrade again must not
            # double-encrypt — already-encrypted rows are skipped by the WHERE NOT LIKE guard.
            command.downgrade(config, "20260922_0013")
            command.upgrade(config, "head")
            with engine.connect() as connection:
                raw2 = connection.execute(text(
                    "SELECT content FROM document_chunks WHERE id = 1"
                )).scalar()
            self.assertEqual(raw2, raw)
            db = QueryDatabase(url, query_field_key="migration-chunk-key")
            self.assertEqual(
                db.document_version_chunks(1, 1)["chunks"][0]["content"], "明文存量正文",
            )
            db.dispose()

            # A fresh downgrade still preserves ciphertext (production never rolls back).
            command.downgrade(config, "20260922_0013")
            with engine.connect() as connection:
                raw3 = connection.execute(text(
                    "SELECT content FROM document_chunks WHERE id = 1"
                )).scalar()
            self.assertTrue(raw3.startswith(PREFIX))
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
