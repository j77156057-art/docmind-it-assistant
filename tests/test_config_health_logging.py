import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import create_app
from backend import AppSettings, JsonFormatter, ModelRouter, configure_logging, request_id_context


class ConfigurationTests(unittest.TestCase):
    def test_dotenv_paths_are_project_relative_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            (project / ".env").write_text(
                "IT_PORT=8123\nIT_DATABASE_PATH=state/it.db\nIT_LOG_JSON=false\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"IT_PORT": "8234"}, clear=False):
                settings = AppSettings.from_environment(project)
            self.assertEqual(settings.port, 8234)
            self.assertEqual(settings.database_path, (project / "state" / "it.db").resolve())
            self.assertFalse(settings.log_json)

    def test_invalid_environment_and_port_fail_fast(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"IT_ENVIRONMENT": "unknown", "IT_PORT": "70000"}, clear=False):
                with self.assertRaises(ValidationError):
                    AppSettings.from_environment(root)

    def test_public_settings_contain_no_credentials(self):
        settings = AppSettings()
        payload = repr(settings.public()).lower()
        self.assertNotIn("api_key", payload)
        self.assertNotIn("secret", payload)

    def test_dotenv_credential_presence_is_tracked_without_storing_value(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            (project / ".env").write_text("DASHSCOPE_API_KEY=private-value\n", encoding="utf-8")
            settings = AppSettings.from_environment(project)
        self.assertTrue(settings.credential_is_configured("DASHSCOPE_API_KEY"))
        self.assertEqual(settings.credential_value("DASHSCOPE_API_KEY"), "private-value")
        self.assertNotIn("private-value", repr(settings))
        self.assertNotIn("configured_credentials", settings.public())


class HealthAndLoggingTests(unittest.TestCase):
    def make_settings(self, root: str, *, knowledge: bool = True, web: bool = True) -> AppSettings:
        project = Path(root)
        knowledge_path = project / "knowledge.md"
        web_path = project / "web" / "index.html"
        if knowledge:
            knowledge_path.write_text("# Demo\n## VPN\n请重试。\n", encoding="utf-8")
        if web:
            web_path.parent.mkdir(parents=True, exist_ok=True)
            web_path.write_text("<!doctype html><title>IT</title>", encoding="utf-8")
        return AppSettings(
            project_root=project,
            environment="test",
            database_path=project / "data" / "queries.db",
            database_url=f"sqlite:///{(project / 'data' / 'queries.db').as_posix()}",
            knowledge_path=knowledge_path,
            web_index_path=web_path,
            log_level="CRITICAL",
        )

    def test_liveness_readiness_and_request_id(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root))
            with TestClient(application) as client:
                live = client.get("/health/live", headers={"X-Request-ID": "check-123"})
                ready = client.get("/health/ready")
            self.assertEqual(live.status_code, 200)
            self.assertEqual(live.headers["X-Request-ID"], "check-123")
            self.assertEqual(ready.status_code, 200)
            self.assertTrue(ready.json()["checks"]["database"]["ok"])

    def test_readiness_is_503_when_knowledge_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root, knowledge=False))
            with TestClient(application) as client:
                response = client.get("/health/ready")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["checks"]["knowledge"]["reason"], "knowledge_missing")

    def test_cloud_readiness_requires_provider_key(self):
        with patch.dict(os.environ, {}, clear=True):
            router = ModelRouter("cloud", cloud_provider="qwen", cloud_model="qwen-plus")
            self.assertEqual(router.healthcheck(), (False, "model_api_key_missing"))

    def test_cloud_readiness_accepts_injected_credential_presence(self):
        router = ModelRouter(
            "cloud", cloud_provider="qwen", cloud_model="qwen-plus",
            configured_credentials=frozenset({"DASHSCOPE_API_KEY"}),
        )
        self.assertEqual(router.healthcheck(), (True, "ok"))

    def test_noisy_http_loggers_are_reduced_to_warning(self):
        configure_logging(AppSettings(environment="test", log_level="INFO"))
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertEqual(logging.getLogger("httpcore").level, logging.WARNING)

    def test_request_log_contains_metadata_but_not_question_or_query_string(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root))
            with patch("app.log_event") as event:
                with TestClient(application) as client:
                    response = client.post(
                        "/api/query?debug=secret-query-param",
                        json={"session_id": "private-session", "question": "password-is-secret"},
                    )
            self.assertEqual(response.status_code, 200)
            rendered = repr(event.call_args_list)
            self.assertIn("request_completed", rendered)
            self.assertIn("/api/query", rendered)
            self.assertNotIn("password-is-secret", rendered)
            self.assertNotIn("secret-query-param", rendered)
            self.assertNotIn("private-session", rendered)

    def test_json_formatter_emits_request_id(self):
        formatter = JsonFormatter()
        token = request_id_context.set("req-unit-test")
        try:
            record = logging.LogRecord("it", logging.INFO, __file__, 1, "ignored", (), None)
            record.event = "unit_event"
            payload = json.loads(formatter.format(record))
        finally:
            request_id_context.reset(token)
        self.assertEqual(payload["request_id"], "req-unit-test")
        self.assertEqual(payload["event"], "unit_event")


if __name__ == "__main__":
    unittest.main()
