"""Connection-pool behaviour under concurrent write load (debt #1 / proposal §9.1 decision C).

Why this file exists
--------------------
``docs/proposal_async_handler_def_ify.md`` §9.1 records the question the ``async def`` → ``def``
migration left open: do ``database_pool_size`` / ``database_max_overflow`` need raising? It could
not be answered locally, because the SQLite branch of ``QueryDatabase`` never passes those
parameters to ``create_engine`` (it builds a ``NullPool``), so on a developer machine there is no
pool to saturate. The pool half of the question needs a real PostgreSQL.

``PROJECT_STATUS.md`` separately lists concurrency as **unverified**: the existing
``tests/test_stress_concurrency.py`` hammers ``GET /api/runtime/model``, a route that never
reaches SQLAlchemy, so it proves the metrics middleware counts requests — not that the pool
bounds anything.

This module closes both gaps where a real pool actually exists:

1. **The configured pool is really built.** On PostgreSQL ``pool_size`` / ``max_overflow`` reach
   ``create_engine``, and the engine ends up with a ``QueuePool`` of the configured size.
2. **The pool bounds concurrent database work.** Across a burst of concurrent write requests, the
   peak number of simultaneously checked-out connections never exceeds
   ``pool_size + max_overflow``. That is the "bounded, observable" claim §5 of the proposal makes:
   above the ceiling, requests wait for a connection instead of piling onto the database.
3. **The burst is genuine contention.** Peak checkouts rise *above* ``pool_size``, so the overflow
   slots are actually used rather than the run being accidentally serialised.
4. **Nothing is lost.** Every concurrent request answers 200 and ``queries`` holds exactly one row
   per request.

Scope — what this does NOT prove
--------------------------------
It is a single-process, ``TestClient``-driven burst over tiny data. It says nothing about
production throughput, multi-worker behaviour, or latency targets, and it must not be quoted as a
capacity number. It also asserts nothing about *how long* any query takes: SQL timings are written
to the report for humans, deliberately not turned into a timing gate (a wall-clock threshold in CI
is a flaky gate, not evidence).

Running it
----------
Set ``IT_TEST_POSTGRES_URL`` to a server the current role may create databases on; the module then
runs against a disposable database it creates and drops itself, exactly like
``tests/test_indexing_graph_postgres.py``. Without that variable the PostgreSQL class skips and only
the SQLite pool-shape test runs, so the default suite stays green on a machine with no PostgreSQL::

    IT_TEST_POSTGRES_URL=postgresql+psycopg://docmind:change-me@127.0.0.1:5432/docmind_it \\
        python -B -m pytest -q tests/test_connection_pool.py

Set ``IT_POOL_REPORT`` to a path to also write the measured numbers as JSON; CI sets it and uploads
the file as an artifact (see .github/workflows/tests.yml).
"""
from __future__ import annotations

import json
import os
import statistics
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.pool import NullPool, QueuePool

from app import create_app
from backend import AppSettings, QueryDatabase


ROOT = Path(__file__).resolve().parents[1]
POSTGRES_URL = os.environ.get("IT_TEST_POSTGRES_URL", "").strip()
REPORT_PATH = os.environ.get("IT_POOL_REPORT", "").strip()

# Mirrors the IT_SLOW_DB_MS default. Used only to label the report, never to gate the test.
SLOW_DB_MS = 200
# anyio's default CapacityLimiter, which is what gates every `def` route handler. Recorded in the
# report because it is the *other* bound on concurrency; the pool is deliberately made smaller so
# that this test measures the pool, not the thread limiter.
ANYIO_DEFAULT_THREAD_TOKENS = 40


def server_url(database_url: str, database: str) -> str:
    """Same server, different database. Used for CREATE/DROP DATABASE."""
    parts = urlsplit(database_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


def connect(database_url: str):
    """psycopg connection for the maintenance statements SQLAlchemy cannot run."""
    from psycopg import connect as psycopg_connect

    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg_connect(dsn, autocommit=True)


def percentile(values: list, fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


class PoolProbe:
    """Records pool occupancy and per-statement SQL timings for one engine.

    Occupancy is measured through SQLAlchemy's own checkout/checkin events rather than by counting
    calls into the repository: that way the number is "connections in use", which is exactly what
    the pool bounds, and it stays correct regardless of which repository method a route happens to
    call. Recording is gated on :attr:`active` so the assertions' own queries are not counted.
    """

    def __init__(self, engine, slow_db_ms: int = SLOW_DB_MS) -> None:
        self._lock = threading.Lock()
        self.active = False
        self.live = 0
        self.peak = 0
        self.slow_db_ms = slow_db_ms
        self.statement_ms: list = []
        self._starts: dict = {}

        event.listen(engine, "checkout", self._on_checkout)
        event.listen(engine, "checkin", self._on_checkin)
        event.listen(engine, "before_cursor_execute", self._before_execute)
        event.listen(engine, "after_cursor_execute", self._after_execute)

    def _on_checkout(self, _connection, _record, _proxy) -> None:
        with self._lock:
            self.live += 1
            if self.active:
                self.peak = max(self.peak, self.live)

    def _on_checkin(self, _connection, _record) -> None:
        with self._lock:
            self.live -= 1

    def _before_execute(self, _connection, _cursor, _statement, _parameters, _context, _many) -> None:
        if self.active:
            self._starts[threading.get_ident()] = time.perf_counter()

    def _after_execute(self, _connection, _cursor, _statement, _parameters, _context, _many) -> None:
        started = self._starts.pop(threading.get_ident(), None)
        if started is None:
            return
        with self._lock:
            self.statement_ms.append((time.perf_counter() - started) * 1000.0)

    def slow_statement_count(self) -> int:
        with self._lock:
            return sum(1 for value in self.statement_ms if value >= self.slow_db_ms)


class SqlitePoolShapeTests(unittest.TestCase):
    """Why the pool half of decision C is unobservable on a developer machine.

    Pinned as a test rather than a comment so that "SQLite has no pool to saturate" cannot quietly
    stop being true: if someone later passes ``pool_size`` through on SQLite, this fails and the
    proposal's §9.1 reasoning has to be revisited.
    """

    def test_sqlite_engine_uses_nullpool_and_ignores_pool_size_parameters(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            (project / "queries.db").write_text("", encoding="utf-8")
            database = QueryDatabase(
                f"sqlite:///{(project / 'queries.db').as_posix()}",
                pool_size=3, max_overflow=5,
            )
            try:
                self.assertEqual(database.backend, "sqlite")
                self.assertIsInstance(database.engine.pool, NullPool)
            finally:
                database.dispose()


@unittest.skipUnless(POSTGRES_URL, "IT_TEST_POSTGRES_URL is not set")
class PostgresConnectionPoolTests(unittest.TestCase):
    """One disposable database for the whole class; the burst is the unit of work."""

    POOL_SIZE = 3
    MAX_OVERFLOW = 5
    # Deliberately above the anyio thread limiter (40) so that the request burst cannot be
    # satisfied by the pool in one wave: every wave must queue for a connection.
    CONCURRENCY = 64
    REQUESTS = 128

    def setUp(self):
        if not POSTGRES_URL.startswith("postgresql"):
            self.skipTest("IT_TEST_POSTGRES_URL must be a postgresql:// URL")
        self.database_name = f"docmind_pool_{os.getpid()}_{os.urandom(3).hex()}"
        self.database_url = server_url(POSTGRES_URL, self.database_name)
        with connect(server_url(POSTGRES_URL, "postgres")) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f'CREATE DATABASE "{self.database_name}"')
        self.alembic_config = Config(str(ROOT / "alembic.ini"))
        self.alembic_config.attributes["database_url"] = self.database_url
        command.upgrade(self.alembic_config, "head")

    def tearDown(self):
        if not self.database_name:
            return
        with connect(server_url(POSTGRES_URL, "postgres")) as connection:
            with connection.cursor() as cursor:
                try:
                    cursor.execute(f'DROP DATABASE IF EXISTS "{self.database_name}" WITH (FORCE)')
                except Exception:  # noqa: BLE001 - fall back on older servers
                    cursor.execute(f'DROP DATABASE IF EXISTS "{self.database_name}"')
        self.database_name = ""

    # -- helpers ---------------------------------------------------------------
    def make_settings(self, root: str) -> AppSettings:
        project = Path(root)
        knowledge = project / "knowledge.md"
        knowledge.write_text("# IT\n## VPN 连接失败\n同步设备时间后重试 VPN。\n", encoding="utf-8")
        web = project / "web" / "index.html"
        web.parent.mkdir(parents=True, exist_ok=True)
        web.write_text("<!doctype html>", encoding="utf-8")
        return AppSettings(
            project_root=project,
            environment="test",
            database_url=self.database_url,
            knowledge_path=knowledge,
            web_index_path=web,
            artifact_output_path=project / "artifacts",
            auth_mode="trusted_headers",
            auth_subject_salt="pool-test-subject-salt",
            log_level="CRITICAL",
            governance_mode="direct",
            # The pool is the subject: a small one makes the ceiling visible within a short burst.
            database_pool_size=self.POOL_SIZE,
            database_max_overflow=self.MAX_OVERFLOW,
            # Quota is orthogonal here and would turn part of the burst into 429s.
            query_daily_quota=0,
            # Silence the app's own slow-query log; the probe measures timings independently.
            slow_db_ms=0,
        )

    def burst(self, client) -> tuple:
        """Fire REQUESTS concurrent write requests; return (statuses, latencies_ms)."""
        def fire(_index: int):
            started = time.perf_counter()
            response = client.post(
                "/api/query",
                json={"session_id": "pool-burst", "question": "VPN 连接失败怎么办"},
                headers={"X-Auth-Subject": "pool-user", "X-Auth-Roles": "viewer"},
            )
            return response.status_code, (time.perf_counter() - started) * 1000.0

        with ThreadPoolExecutor(max_workers=self.CONCURRENCY) as pool:
            results = list(pool.map(fire, range(self.REQUESTS)))
        return [code for code, _ in results], [ms for _, ms in results]

    def count_queries_rows(self) -> int:
        database = QueryDatabase(self.database_url)
        try:
            with database.engine.connect() as connection:
                return int(connection.execute(text("SELECT count(*) FROM queries")).scalar_one())
        finally:
            database.dispose()

    # -- test ------------------------------------------------------------------
    def test_concurrent_write_burst_stays_within_the_connection_pool(self):
        with tempfile.TemporaryDirectory() as raw:
            settings = self.make_settings(raw)
            application = create_app(settings)
            with TestClient(application) as client:
                database = application.state.database
                pool = database.engine.pool
                self.assertIsInstance(
                    pool, QueuePool,
                    "PostgreSQL must build a real pool; pool_size/max_overflow are only passed "
                    "to create_engine on the postgresql branch of QueryDatabase",
                )
                self.assertEqual(pool.size(), self.POOL_SIZE)
                # Private attribute: QueuePool exposes no public max_overflow accessor, and the
                # whole point is to prove the constructor argument arrived.
                self.assertEqual(pool._max_overflow, self.MAX_OVERFLOW)

                probe = PoolProbe(database.engine, slow_db_ms=SLOW_DB_MS)
                probe.active = True
                started = time.perf_counter()
                statuses, latencies = self.burst(client)
                wall = time.perf_counter() - started
                probe.active = False

            failures = [code for code in statuses if code != 200]
            self.assertEqual(
                failures, [],
                f"{len(failures)}/{self.REQUESTS} concurrent requests did not return 200 "
                f"(distinct codes: {sorted(set(failures))}); a pool timeout surfaces here as 500",
            )
            # Genuine contention: the overflow slots were used, not just the base pool.
            self.assertGreater(
                probe.peak, self.POOL_SIZE,
                f"peak concurrent checkouts was {probe.peak}, so the burst never exceeded the base "
                f"pool of {self.POOL_SIZE} and proved nothing about overflow",
            )
            # The invariant §5 of the proposal claims: concurrency is bounded, not unbounded.
            capacity = self.POOL_SIZE + self.MAX_OVERFLOW
            self.assertLessEqual(
                probe.peak, capacity,
                f"peak concurrent checkouts {probe.peak} exceeded pool capacity {capacity}; "
                f"requests are reaching the database outside the pool",
            )
            rows = self.count_queries_rows()
            self.assertEqual(
                rows, self.REQUESTS,
                f"queries holds {rows} rows for {self.REQUESTS} requests — writes were lost or "
                f"duplicated under concurrency",
            )

        report = {
            "backend": "postgresql",
            "pool_class": type(pool).__name__,
            "pool_size": self.POOL_SIZE,
            "max_overflow": self.MAX_OVERFLOW,
            "pool_capacity": capacity,
            "anyio_default_thread_tokens": ANYIO_DEFAULT_THREAD_TOKENS,
            "offered_concurrency": self.CONCURRENCY,
            "requests": self.REQUESTS,
            "requests_ok": len(statuses) - len(failures),
            "failures": len(failures),
            "peak_concurrent_checkouts": probe.peak,
            "queries_rows": rows,
            "wall_seconds": round(wall, 3),
            "throughput_rps": round(self.REQUESTS / wall, 1) if wall else 0.0,
            "http_latency_ms": {
                "p50": round(percentile(latencies, 0.50), 1),
                "p95": round(percentile(latencies, 0.95), 1),
                "max": round(max(latencies), 1),
                "mean": round(statistics.mean(latencies), 1),
            },
            "sql_ms": {
                "statements": len(probe.statement_ms),
                "p50": round(percentile(probe.statement_ms, 0.50), 2),
                "p95": round(percentile(probe.statement_ms, 0.95), 2),
                "max": round(max(probe.statement_ms or [0.0]), 2),
                "slow_db_ms_threshold": SLOW_DB_MS,
                "statements_over_threshold": probe.slow_statement_count(),
            },
        }
        if REPORT_PATH:
            Path(REPORT_PATH).write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
            )
        print("pool concurrency report: " + json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
