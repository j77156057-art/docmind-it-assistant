import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app import app
from assistant import ITQueryService
from backend import ModelRouter, QueryDatabase


class ITAssistantTests(unittest.TestCase):
    def test_query_only_service_and_database_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            knowledge = os.path.join(root, "knowledge.md")
            with open(knowledge, "w", encoding="utf-8") as stream:
                stream.write("# Demo\n## VPN 连接失败\n请同步设备时间后重试 VPN。\n")
            database = QueryDatabase(os.path.join(root, "queries.db"))
            service = ITQueryService(knowledge, database, ModelRouter("knowledge"))
            result = service.query("same", "VPN 连接失败")
            self.assertEqual(result["evidence"], "sufficient")
            self.assertEqual(result["model"]["route"], "knowledge")
            self.assertEqual(len(database.history("same")), 1)
            self.assertEqual(database.history("other"), [])
            self.assertFalse(hasattr(service, "create_file"))
            self.assertFalse(hasattr(service, "run_command"))

    def test_greeting_and_knowledge_overview_stay_on_builtin_route(self):
        with tempfile.TemporaryDirectory() as root:
            knowledge = os.path.join(root, "knowledge.md")
            with open(knowledge, "w", encoding="utf-8") as stream:
                stream.write(
                    "# Demo\n\n> 仅用于演示。\n\n"
                    "## VPN 连接失败\n同步设备时间。\n\n"
                    "## 账号锁定与密码\n使用自助解锁。\n"
                )
            database = QueryDatabase(os.path.join(root, "queries.db"))
            service = ITQueryService(
                knowledge, database,
                ModelRouter("local", local_provider="ollama", local_model="qwen2.5:7b"),
            )

            greeting = service.query("intent", "您好！")
            overview = service.query("intent", "请问知识库里讲了什么")

        self.assertEqual(greeting["model"]["route"], "knowledge")
        self.assertIsNone(greeting["usage"])
        self.assertIn("DocMind", greeting["answer"])
        self.assertEqual(overview["model"]["route"], "knowledge")
        self.assertIn("VPN 连接失败", overview["answer"])
        self.assertIn("账号锁定与密码", overview["answer"])
        self.assertNotIn("仅用于演示", overview["answer"])

    def test_http_query_and_model_status(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/runtime/model").status_code, 200)
            self.assertEqual(client.post("/api/query", json={"question": ""}).status_code, 400)
            self.assertEqual(client.get("/api/admin/documents").status_code, 404)


if __name__ == "__main__":
    unittest.main()
