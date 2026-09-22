"""Per-user daily query rate limiting (Phase 5, A-4)."""
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from backend.config import AppSettings
from backend.ratelimit import QueryRateLimiter


class TestQueryRateLimiter:
    def test_disabled_when_quota_zero(self):
        limiter = QueryRateLimiter(0)
        assert limiter.enabled is False
        for _ in range(5):
            allowed, remaining, retry = limiter.hit("u")
            assert allowed is True
            assert remaining == 0
            assert retry == 0

    def test_allows_up_to_quota_then_blocks(self):
        limiter = QueryRateLimiter(2)
        allowed, remaining, _ = limiter.hit("u")
        assert allowed is True and remaining == 1
        allowed, remaining, _ = limiter.hit("u")
        assert allowed is True and remaining == 0
        allowed, remaining, retry = limiter.hit("u")
        assert allowed is False and remaining == 0 and retry > 0

    def test_retry_after_points_to_next_utc_midnight(self):
        limiter = QueryRateLimiter(1)
        limiter.hit("u")
        _, _, retry = limiter.hit("u")
        # Seconds until next UTC day boundary must be within (0, 24h].
        assert 0 < retry <= 24 * 3600

    def test_subjects_are_independent(self):
        limiter = QueryRateLimiter(1)
        assert limiter.hit("a")[0] is True
        assert limiter.hit("b")[0] is True  # separate budget
        assert limiter.hit("a")[0] is False

    def test_day_rollover_resets_counts(self):
        limiter = QueryRateLimiter(1)
        assert limiter.hit("u")[0] is True
        assert limiter.hit("u")[0] is False
        # Simulate the UTC day rolling over.
        limiter._day = "2000-01-01"
        assert limiter.hit("u")[0] is True


class TestQueryRateLimitEndpoint:
    def _settings(self, root: str, quota: int) -> AppSettings:
        project = Path(root)
        knowledge = project / "knowledge.md"
        knowledge.write_text("# IT\n## VPN\n请重新登录 VPN。\n", encoding="utf-8")
        web = project / "web" / "index.html"
        web.parent.mkdir(parents=True, exist_ok=True)
        web.write_text("<!doctype html>", encoding="utf-8")
        return AppSettings(
            project_root=project,
            environment="test",
            database_url=f"sqlite:///{(project / 'queries.db').as_posix()}",
            knowledge_path=knowledge,
            web_index_path=web,
            artifact_output_path=project / "artifacts",
            auth_mode="trusted_headers",
            auth_subject_salt="unit-test-subject-salt",
            query_daily_quota=quota,
            log_level="CRITICAL",
        )

    def _headers(self, subject: str) -> dict[str, str]:
        return {"X-Auth-Subject": subject, "X-Auth-Roles": "viewer"}

    def test_exceeding_daily_quota_returns_429_with_retry_after(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_app(self._settings(root, quota=2))
            with TestClient(app) as client:
                first = client.post("/api/query", json={"question": "VPN 怎么配"}, headers=self._headers("user-1"))
                second = client.post("/api/query", json={"question": "DNS 是什么"}, headers=self._headers("user-1"))
                third = client.post("/api/query", json={"question": "再次提问"}, headers=self._headers("user-1"))
            assert first.status_code == 200
            assert second.status_code == 200
            assert third.status_code == 429
            assert third.headers.get("Retry-After", "").isdigit()
            assert third.headers.get("X-RateLimit-Remaining") == "0"
            assert third.headers.get("X-RateLimit-Limit") == "2"
            body = third.json()
            assert body["ok"] is False and "retry_after_seconds" in body

    def test_other_subjects_keep_their_own_budget(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_app(self._settings(root, quota=1))
            with TestClient(app) as client:
                a = client.post("/api/query", json={"question": "问题 A"}, headers=self._headers("user-a"))
                b = client.post("/api/query", json={"question": "问题 B"}, headers=self._headers("user-b"))
                a_again = client.post("/api/query", json={"question": "问题 A2"}, headers=self._headers("user-a"))
            assert a.status_code == 200
            assert b.status_code == 200  # independent budget
            assert a_again.status_code == 429
