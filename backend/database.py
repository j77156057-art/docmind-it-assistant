"""SQLAlchemy query repository supporting PostgreSQL and isolated SQLite tests."""
from __future__ import annotations

from pathlib import Path

from decimal import Decimal

from sqlalchemy import case, create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from .db_models import Base, ModelUsageRecord, QueryRecord
from .pricing import cost_cny, pricing_status


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
            inspector = inspect(self.engine)
            if not inspector.has_table("alembic_version"):
                Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()

    def healthcheck(self) -> tuple[bool, str]:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            tables = set(inspect(self.engine).get_table_names())
            if QueryRecord.__tablename__ not in tables or ModelUsageRecord.__tablename__ not in tables:
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

    def record_model_attempts(self, query_id: int, request_id: str, route: dict, attempts) -> None:
        pricing = pricing_status(route["provider"], route["model"])
        input_price = Decimal(str(pricing["input"])) if pricing["known"] else None
        output_price = Decimal(str(pricing["output"])) if pricing["known"] else None
        rows = []
        for attempt in attempts:
            charge = None
            if attempt.usage_reported and pricing["known"]:
                charge = Decimal(str(cost_cny(
                    route["provider"], route["model"],
                    attempt.prompt_tokens or 0, attempt.completion_tokens or 0,
                )))
            rows.append(ModelUsageRecord(
                query_id=query_id,
                request_id=request_id[:128],
                provider=route["provider"],
                model=route["model"][:128],
                route=route["route"],
                attempt=attempt.attempt,
                status=attempt.status,
                prompt_tokens=attempt.prompt_tokens,
                completion_tokens=attempt.completion_tokens,
                total_tokens=attempt.total_tokens,
                usage_reported=attempt.usage_reported,
                input_price_cny=input_price,
                output_price_cny=output_price,
                cost_cny=charge,
                provider_request_id=attempt.provider_request_id or None,
                latency_ms=attempt.latency_ms,
                error_code=attempt.error_code,
            ))
        if rows:
            with self._sessions.begin() as session:
                session.add_all(rows)

    def usage_ledger(self, session_id: str, limit: int = 50) -> list[dict]:
        count = max(1, min(int(limit), 100))
        statement = (
            select(ModelUsageRecord)
            .join(QueryRecord, QueryRecord.id == ModelUsageRecord.query_id)
            .where(QueryRecord.session_id == str(session_id or "default")[:128])
            .order_by(ModelUsageRecord.id.desc())
            .limit(count)
        )
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [self._usage_public(row) for row in rows]

    def usage_summary(self, session_id: str) -> dict:
        statement = (
            select(
                func.count(ModelUsageRecord.id),
                func.sum(case((ModelUsageRecord.status == "succeeded", 1), else_=0)),
                func.coalesce(func.sum(ModelUsageRecord.prompt_tokens), 0),
                func.coalesce(func.sum(ModelUsageRecord.completion_tokens), 0),
                func.coalesce(func.sum(ModelUsageRecord.total_tokens), 0),
                func.coalesce(func.sum(ModelUsageRecord.cost_cny), 0),
                func.sum(case((
                    (ModelUsageRecord.status == "succeeded")
                    & ModelUsageRecord.usage_reported
                    & (ModelUsageRecord.cost_cny.is_(None)), 1
                ), else_=0)),
                func.sum(case((
                    (ModelUsageRecord.status == "succeeded")
                    & (ModelUsageRecord.usage_reported.is_(False)), 1
                ), else_=0)),
            )
            .join(QueryRecord, QueryRecord.id == ModelUsageRecord.query_id)
            .where(QueryRecord.session_id == str(session_id or "default")[:128])
        )
        with self._sessions() as session:
            row = session.execute(statement).one()
        return {
            "attempts": int(row[0] or 0),
            "successful_calls": int(row[1] or 0),
            "prompt_tokens": int(row[2] or 0),
            "completion_tokens": int(row[3] or 0),
            "total_tokens": int(row[4] or 0),
            "cost_cny": float(row[5] or 0),
            "unpriced_calls": int(row[6] or 0),
            "unmetered_calls": int(row[7] or 0),
            "currency": "CNY",
        }

    @staticmethod
    def _usage_public(row: ModelUsageRecord) -> dict:
        return {
            "id": row.id,
            "query_id": row.query_id,
            "request_id": row.request_id,
            "provider": row.provider,
            "model": row.model,
            "route": row.route,
            "attempt": row.attempt,
            "status": row.status,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "total_tokens": row.total_tokens,
            "usage_reported": row.usage_reported,
            "cost_cny": float(row.cost_cny) if row.cost_cny is not None else None,
            "input_price_cny_per_million": (
                float(row.input_price_cny) if row.input_price_cny is not None else None
            ),
            "output_price_cny_per_million": (
                float(row.output_price_cny) if row.output_price_cny is not None else None
            ),
            "provider_request_id": row.provider_request_id,
            "latency_ms": row.latency_ms,
            "error_code": row.error_code,
            "created_at": row.created_at.isoformat(),
        }
