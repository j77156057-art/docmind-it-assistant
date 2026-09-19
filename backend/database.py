"""SQLAlchemy query repository supporting PostgreSQL and isolated SQLite tests."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from .db_models import Base, QueryRecord


def _database_url(value: str) -> str:
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    if "://" in value:
        return value
    return f"sqlite:///{Path(value).resolve().as_posix()}"


class QueryDatabase:
    """Repository boundary; PostgreSQL schema ownership remains with Alembic."""

    def __init__(self, url_or_path: str, *, pool_size: int = 5, max_overflow: int = 10,
                 pool_timeout: int = 30, connect_timeout: int = 5):
        self.url = _database_url(str(url_or_path))
        url = make_url(self.url)
        engine_options: dict = {"pool_pre_ping": True}
        if url.get_backend_name() == "sqlite":
            database = url.database or ""
            if database and database != ":memory:":
                Path(database).parent.mkdir(parents=True, exist_ok=True)
            engine_options["connect_args"] = {"check_same_thread": False}
            engine_options["poolclass"] = NullPool
        elif url.get_backend_name() == "postgresql":
            engine_options.update({
                "pool_size": pool_size,
                "max_overflow": max_overflow,
                "pool_timeout": pool_timeout,
                "connect_args": {"connect_timeout": connect_timeout},
            })
        self.engine: Engine = create_engine(self.url, **engine_options)
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    @property
    def backend(self) -> str:
        return self.engine.url.get_backend_name()

    def initialize(self) -> None:
        """Create only disposable SQLite schemas; PostgreSQL uses Alembic exclusively."""
        if self.backend == "sqlite":
            Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()

    def healthcheck(self) -> tuple[bool, str]:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            if not inspect(self.engine).has_table(QueryRecord.__tablename__):
                return False, "database_schema_missing"
            return True, "ok"
        except (OSError, SQLAlchemyError):
            return False, "database_unavailable"

    def record(self, session_id: str, question: str, evidence: str, model_route: str) -> int:
        self.initialize()
        row = QueryRecord(
            session_id=str(session_id or "default")[:128],
            question=question,
            evidence=evidence,
            model_route=model_route,
        )
        with self._sessions.begin() as session:
            session.add(row)
        return int(row.id)

    def history(self, session_id: str, limit: int = 20) -> list[dict]:
        self.initialize()
        count = max(1, min(int(limit), 100))
        statement = (
            select(QueryRecord)
            .where(QueryRecord.session_id == str(session_id or "default")[:128])
            .order_by(QueryRecord.id.desc())
            .limit(count)
        )
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [
            {
                "id": row.id,
                "question": row.question,
                "evidence": row.evidence,
                "model_route": row.model_route,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
