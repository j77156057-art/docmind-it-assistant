"""SQLite transport owned by the backend, not by the assistant UI."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone


class QueryDatabase:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)

    def initialize(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS queries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        question TEXT NOT NULL,
                        evidence TEXT NOT NULL,
                        model_route TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)

    def healthcheck(self) -> tuple[bool, str]:
        try:
            with closing(sqlite3.connect(self.path, timeout=2.0)) as db:
                value = db.execute("SELECT 1").fetchone()
            return (bool(value and value[0] == 1), "ok")
        except (OSError, sqlite3.Error):
            return False, "database_unavailable"

    def record(self, session_id: str, question: str, evidence: str, model_route: str) -> int:
        self.initialize()
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                cursor = db.execute(
                    "INSERT INTO queries(session_id, question, evidence, model_route, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (str(session_id or "default"), question, evidence, model_route, created_at),
                )
                return int(cursor.lastrowid)

    def history(self, session_id: str, limit: int = 20) -> list[dict]:
        self.initialize()
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT id, question, evidence, model_route, created_at FROM queries "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (str(session_id or "default"), max(1, min(int(limit), 100))),
            ).fetchall()
        return [dict(row) for row in rows]
