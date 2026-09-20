import json
import unittest

import httpx

from backend import ModelRuntime, ModelRuntimeError


class ModelRuntimeTests(unittest.TestCase):
    def test_lists_all_installed_ollama_models_without_loading_them(self):
        def handler(request: httpx.Request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.path, "/api/tags")
            return httpx.Response(200, json={"models": [
                {"name": "qwen3:8b"}, {"name": "qwen2.5:7b"}, {"name": "qwen3:8b"},
            ]})

        result = ModelRuntime(transport=httpx.MockTransport(handler)).available_models(
            "ollama", "http://127.0.0.1:11434/v1",
        )
        self.assertEqual(result["models"], ["qwen2.5:7b", "qwen3:8b"])
        self.assertTrue(result["reachable"])

    def test_ollama_activation_runs_real_probe_and_confirms_residency(self):
        calls = []

        def handler(request: httpx.Request):
            calls.append((request.method, request.url.path))
            if request.url.path == "/api/generate":
                payload = json.loads(request.content)
                self.assertEqual(payload["model"], "qwen2.5:7b")
                self.assertEqual(payload["keep_alive"], "24h")
                return httpx.Response(200, json={"response": "OK", "done": True})
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]})
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [{
                    "name": "qwen2.5:7b", "size_vram": 3 * 1024 ** 3,
                }]})
            return httpx.Response(404)

        runtime = ModelRuntime(transport=httpx.MockTransport(handler))
        result = runtime.activate(
            {"mode": "local", "provider": "ollama", "model": "qwen2.5:7b"},
            "http://127.0.0.1:11434/v1",
        )

        self.assertTrue(result["verified"])
        self.assertTrue(result["ready"])
        self.assertTrue(result["loaded"])
        self.assertEqual(result["vram_gb"], 3.0)
        self.assertEqual(calls, [
            ("POST", "/api/generate"), ("GET", "/api/tags"), ("GET", "/api/ps"),
        ])

    def test_ollama_activation_failure_does_not_look_ready(self):
        def handler(_request: httpx.Request):
            raise httpx.ConnectError("offline")

        runtime = ModelRuntime(transport=httpx.MockTransport(handler))
        with self.assertRaisesRegex(ModelRuntimeError, "无法连接"):
            runtime.activate(
                {"mode": "local", "provider": "ollama", "model": "missing"},
                "http://127.0.0.1:11434/v1",
            )


if __name__ == "__main__":
    unittest.main()
