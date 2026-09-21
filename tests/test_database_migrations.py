from pathlib import Path
import tempfile
import unittest

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from pydantic import ValidationError

from backend import AppSettings, QueryDatabase


ROOT = Path(__file__).resolve().parents[1]


def migration_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


class DatabaseMigrationTests(unittest.TestCase):
    def test_upgrade_repository_and_downgrade(self):
        with tempfile.TemporaryDirectory() as root:
            url = f"sqlite:///{(Path(root) / 'migration.db').as_posix()}"
            config = migration_config(url)
            command.upgrade(config, "head")
            command.check(config)

            database = QueryDatabase(url)
            self.assertEqual(database.healthcheck(), (True, "ok"))
            query_id = database.record("migration", "VPN?", "sufficient", "knowledge")
            self.assertGreater(query_id, 0)
            self.assertEqual(database.history("migration")[0]["question"], "VPN?")
            database.dispose()

            command.downgrade(config, "base")
            engine = create_engine(url)
            self.assertFalse(inspect(engine).has_table("queries"))
            engine.dispose()

    def test_unmigrated_database_is_not_ready(self):
        with tempfile.TemporaryDirectory() as root:
            url = f"sqlite:///{(Path(root) / 'empty.db').as_posix()}"
            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(text("SELECT 1"))
            engine.dispose()
            database = QueryDatabase(url)
            self.assertEqual(database.healthcheck(), (False, "database_schema_missing"))
            database.dispose()

    def test_production_requires_postgresql(self):
        with self.assertRaises(ValidationError):
            AppSettings(environment="production", database_url="sqlite:///unsafe.db")
        settings = AppSettings(
            environment="production",
            database_url="postgresql+psycopg://user:secret@db/docmind",
            embedding_mode="provider",
            auth_mode="oidc",
            oidc_issuer="https://identity.example.com",
            oidc_audience="docmind",
            oidc_jwks_url="https://identity.example.com/.well-known/jwks.json",
            oidc_client_id="docmind-portal",
            oidc_redirect_uri="https://docmind.example.com/api/auth/oidc/callback",
            auth_subject_salt="a-production-subject-salt-at-least-32-characters",
        )
        self.assertNotIn("secret", repr(settings))
        self.assertNotIn("database_url", settings.public())

    def test_governance_migration_backfills_and_downgrade_never_publishes_drafts(self):
        with tempfile.TemporaryDirectory() as root:
            url = f"sqlite:///{(Path(root) / 'governance.db').as_posix()}"
            config = migration_config(url)
            command.upgrade(config, "20260920_0007")

            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO documents (id, source_key, title, mime_type, access_scope,"
                    " classification, created_at, updated_at) VALUES"
                    " (1, 'manual/vpn', 'VPN', 'text/markdown', 'public', 'internal',"
                    " '2026-01-01', '2026-01-01')"
                ))
                connection.execute(text(
                    "INSERT INTO document_versions (id, document_id, version, content_sha256,"
                    " status, chunk_count, created_at, published_at) VALUES"
                    " (1, 1, 1, 'hash-live', 'indexed', 3, '2026-01-01', '2026-01-02'),"
                    " (2, 1, 2, 'hash-queued', 'pending', 0, '2026-01-03', NULL)"
                ))
            engine.dispose()

            command.upgrade(config, "head")
            command.check(config)

            engine = create_engine(url)
            with engine.connect() as connection:
                statuses = connection.execute(text(
                    "SELECT version, status, indexed_at FROM document_versions ORDER BY version"
                )).all()
                reviews = connection.execute(text(
                    "SELECT document_version_id, action, actor_subject_id, is_override"
                    " FROM document_version_reviews ORDER BY id"
                )).all()
            engine.dispose()

            self.assertEqual([(row[0], row[1]) for row in statuses],
                             [(1, "indexed"), (2, "queued")])
            self.assertIsNotNone(statuses[0][2])
            self.assertIsNone(statuses[1][2])
            # Every already-live version gets an explained approval record.
            self.assertEqual([(row[0], row[1], row[2], row[3]) for row in reviews],
                             [(1, "publish", "system:backfill", 0)])

            # A staged version exists only in the new vocabulary; the downgrade must not publish it.
            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO document_versions (id, document_id, version, content_sha256,"
                    " status, chunk_count, created_at) VALUES"
                    " (3, 1, 3, 'hash-staged', 'staged', 2, '2026-01-04')"
                ))
            engine.dispose()

            command.downgrade(config, "20260920_0007")
            engine = create_engine(url)
            with engine.connect() as connection:
                downgraded = connection.execute(text(
                    "SELECT version, status FROM document_versions ORDER BY version"
                )).all()
                columns = {row[1] for row in connection.execute(
                    text("PRAGMA table_info(document_versions)")
                )}
            has_reviews = inspect(engine).has_table("document_version_reviews")
            engine.dispose()

            self.assertEqual([(row[0], row[1]) for row in downgraded],
                             [(1, "indexed"), (2, "pending"), (3, "pending")])
            self.assertFalse(has_reviews)
            self.assertNotIn("superseded_by_version_id", columns)
            self.assertNotIn("submitted_by_subject_id", columns)

            command.upgrade(config, "head")
            engine = create_engine(url)
            with engine.connect() as connection:
                self.assertEqual(
                    [row[0] for row in connection.execute(text(
                        "SELECT version FROM document_versions ORDER BY version"
                    ))],
                    [1, 2, 3],
                )
            engine.dispose()

    def test_standard_postgresql_url_uses_psycopg_driver(self):
        database = QueryDatabase("postgresql://user:secret@db/docmind")
        self.assertEqual(database.engine.url.drivername, "postgresql+psycopg")
        database.dispose()


if __name__ == "__main__":
    unittest.main()
