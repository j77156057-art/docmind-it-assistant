import json
from pathlib import Path
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient

from assistant import ITQueryService
from app import create_app
from backend import (
    AppSettings, GatewayAttempt, ModelGateway, ModelGatewayError, ModelRouter, QueryDatabase,
    request_id_context,
)


class RealModelGatewayTests(unittest.TestCase):
    def make_service(self, root: str, handler, *, retries: int = 1):
        project = Path(root)
        knowledge = project / "knowledge.md"
        knowledge.write_text("# Demo\n## VPN\n仅处理 VPN。\n", encoding="utf-8")
        database = QueryDatabase(str(project / "queries.db"))
        router = ModelRouter(
            "cloud",
            cloud_provider="qwen",
            cloud_model="qwen-plus",
            configured_credentials=frozenset({"DASHSCOPE_API_KEY"}),
            credentials={"DASHSCOPE_API_KEY": "gateway-secret"},
        )
        gateway = ModelGateway(
            max_retries=retries, retry_backoff_seconds=0,
            transport=httpx.MockTransport(handler),
        )
        return ITQueryService(str(knowledge), database, router, gateway), database

    def test_real_completion_records_provider_usage_and_cost(self):
        captured = {}

        def handler(request: httpx.Request):
            captured["authorization"] = request.headers.get("authorization")
            captured["request_id"] = request.headers.get("x-request-id")
            captured["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"x-request-id": "provider-request-1"},
                json={
                    "choices": [{"message": {"content": "请先确认账号状态。"}}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
                },
            )

        with tempfile.TemporaryDirectory() as root:
            service, database = self.make_service(root, handler)
            token = request_id_context.set("gateway-test-request")
            try:
                result = service.query("session-a", "账号为何无法登录？")
            finally:
                request_id_context.reset(token)
            ledger = database.usage_ledger("session-a")
            summary = database.usage_summary("session-a")
            database.dispose()

        self.assertEqual(result["answer"], "请先确认账号状态。")
        self.assertEqual(result["usage"]["total_tokens"], 1200)
        self.assertEqual(result["usage"]["cost_cny"], 0.0012)
        self.assertEqual(captured["authorization"], "Bearer gateway-secret")
        self.assertEqual(captured["request_id"], "gateway-test-request")
        self.assertEqual(captured["payload"]["model"], "qwen-plus")
        self.assertEqual(ledger[0]["provider"], "qwen")
        self.assertEqual(ledger[0]["request_id"], "gateway-test-request")
        self.assertEqual(ledger[0]["provider_request_id"], "provider-request-1")
        self.assertEqual(ledger[0]["cost_cny"], 0.0012)
        self.assertEqual(summary["successful_calls"], 1)
        self.assertEqual(summary["total_tokens"], 1200)
        self.assertNotIn("gateway-secret", repr(ledger))
        self.assertNotIn("账号为何无法登录", repr(ledger))

    def test_retry_attempts_are_all_recorded(self):
        calls = 0

        def handler(_request: httpx.Request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"x-request-id": "rate-limited"})
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "第二次成功"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            })

        with tempfile.TemporaryDirectory() as root:
            service, database = self.make_service(root, handler)
            result = service.query("session-b", "打印机出现未知错误")
            ledger = list(reversed(database.usage_ledger("session-b")))
            database.dispose()

        self.assertEqual(result["answer"], "第二次成功")
        self.assertEqual([item["status"] for item in ledger], ["failed", "succeeded"])
        self.assertEqual(ledger[0]["error_code"], "provider_http_429")
        self.assertEqual([item["attempt"] for item in ledger], [1, 2])

    def test_terminal_failure_is_recorded_without_sensitive_response(self):
        def handler(_request: httpx.Request):
            return httpx.Response(503, text="sensitive provider response")

        with tempfile.TemporaryDirectory() as root:
            service, database = self.make_service(root, handler)
            with self.assertRaises(ModelGatewayError):
                service.query("session-c", "无法识别的问题")
            ledger = database.usage_ledger("session-c")
            summary = database.usage_summary("session-c")
            database.dispose()

        self.assertEqual(len(ledger), 2)
        self.assertTrue(all(item["status"] == "failed" for item in ledger))
        self.assertEqual(summary["successful_calls"], 0)
        self.assertNotIn("sensitive provider response", repr(ledger))

    def test_unknown_cloud_price_is_not_reported_as_free(self):
        attempt = GatewayAttempt(1, "succeeded", 3, 2, 5, True, "", 1)
        usage = ModelGateway.public_usage(
            {"provider": "openai", "model": "gpt-4o-mini"}, attempt,
        )
        self.assertIsNone(usage["cost_cny"])
        self.assertFalse(usage["pricing_known"])

    def test_http_query_and_usage_endpoints(self):
        def handler(_request: httpx.Request):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "模型回答"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            })

        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            knowledge = project / "knowledge.md"
            knowledge.write_text("# Empty\n", encoding="utf-8")
            web = project / "web" / "index.html"
            web.parent.mkdir(parents=True)
            web.write_text("<!doctype html>", encoding="utf-8")
            settings = AppSettings(
                project_root=project,
                environment="test",
                database_url=f"sqlite:///{(project / 'queries.db').as_posix()}",
                knowledge_path=knowledge,
                web_index_path=web,
                model_mode="cloud",
                cloud_provider="qwen",
                cloud_model="qwen-plus",
                configured_credentials=frozenset({"DASHSCOPE_API_KEY"}),
                credentials={"DASHSCOPE_API_KEY": "api-test-secret"},
                log_level="CRITICAL",
            )
            gateway = ModelGateway(
                max_retries=0, retry_backoff_seconds=0,
                transport=httpx.MockTransport(handler),
            )
            application = create_app(settings, gateway)
            with TestClient(application) as client:
                response = client.post(
                    "/api/query",
                    headers={"X-Request-ID": "api-model-call"},
                    json={"session_id": "api-session", "question": "未知问题"},
                )
                summary = client.get("/api/usage/summary?session_id=api-session")
                ledger = client.get("/api/usage/ledger?session_id=api-session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["usage"]["total_tokens"], 30)
        self.assertEqual(summary.json()["successful_calls"], 1)
        self.assertEqual(ledger.json()["items"][0]["request_id"], "api-model-call")
        self.assertNotIn("api-test-secret", repr(ledger.json()))


if __name__ == "__main__":
    unittest.main()
