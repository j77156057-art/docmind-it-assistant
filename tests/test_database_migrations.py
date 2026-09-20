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
            auth_subject_salt="a-production-subject-salt-at-least-32-characters",
        )
        self.assertNotIn("secret", repr(settings))
        self.assertNotIn("database_url", settings.public())

    def test_standard_postgresql_url_uses_psycopg_driver(self):
        database = QueryDatabase("postgresql://user:secret@db/docmind")
        self.assertEqual(database.engine.url.drivername, "postgresql+psycopg")
        database.dispose()


if __name__ == "__main__":
    unittest.main()
