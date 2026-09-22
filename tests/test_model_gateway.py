import os
import unittest
from unittest.mock import patch
import tempfile
from pathlib import Path

from backend import ModelRouter, QueryDatabase, cost_cny, get_provider, model_context_window, price_for


class ModelGatewayMigrationTests(unittest.TestCase):
    def test_builtin_route_is_free_and_read_only(self):
        route = ModelRouter("knowledge").select("VPN", "sufficient")
        self.assertEqual(route["provider"], "builtin")
        self.assertFalse(route["cloud"])
        self.assertEqual(route["pricing"]["input"], 0.0)

    def test_cloud_route_uses_provider_catalog_without_exposing_key(self):
        with patch.dict(os.environ, {
            "IT_CLOUD_PROVIDER": "deepseek",
            "IT_CLOUD_MODEL": "deepseek-reasoner",
            "DEEPSEEK_API_KEY": "secret-for-test",
        }, clear=False):
            router = ModelRouter("cloud")
            route = router.select("未知问题", "insufficient")
            status = router.status()
        self.assertEqual(route["provider"], "deepseek")
        self.assertEqual(route["context_window"], 65536)
        self.assertTrue(status["api_key_configured"])
        self.assertNotIn("secret-for-test", repr(status))

    def test_pricing_math_and_local_zero_cost(self):
        self.assertEqual(price_for("qwen", "qwen-plus"), (0.8, 2.0))
        self.assertEqual(cost_cny("qwen", "qwen-plus", 1_000_000, 1_000_000), 2.8)
        self.assertEqual(cost_cny("ollama", "qwen2.5:7b", 5000, 5000), 0.0)

    def test_provider_validation_and_context_defaults(self):
        self.assertEqual(get_provider("ollama").key, "ollama")
        self.assertEqual(model_context_window("ollama", "qwen2.5:7b"), 32768)
        with self.assertRaises(ValueError):
            get_provider("not-supported")
        with self.assertRaises(ValueError):
            ModelRouter("developer")

    def test_runtime_loader_changes_fallback_route_without_restart(self):
        active = {"mode": "local", "provider": "ollama", "model": "qwen2.5:7b"}
        router = ModelRouter("knowledge", runtime_loader=lambda: active)

        local = router.select("未知问题", "insufficient")
        active.update({"mode": "knowledge", "provider": "builtin", "model": "deterministic"})
        knowledge = router.select("未知问题", "insufficient")

        self.assertEqual((local["route"], local["provider"]), ("local", "ollama"))
        self.assertEqual((knowledge["route"], knowledge["provider"]), ("knowledge", "builtin"))
        with self.assertRaisesRegex(ValueError, "API Key"):
            router.validate_selection("cloud", "qwen", "qwen-plus")

    def test_runtime_cloud_credential_is_encrypted_and_loaded(self):
        with tempfile.TemporaryDirectory() as root:
            database = QueryDatabase(str(Path(root) / "credentials.db"), credential_key="test-secret")
            database.initialize()
            database.set_runtime_provider_credential(
                provider="qwen", api_key="secret-cloud-key",
                actor_subject_id="admin", request_id="credential-test",
            )
            router = ModelRouter(
                "cloud", cloud_provider="qwen", cloud_model="qwen-plus",
                runtime_credentials_loader=database.runtime_provider_credentials,
            )
            with database.engine.connect() as connection:
                stored = connection.exec_driver_sql(
                    "SELECT ciphertext FROM runtime_provider_credentials WHERE provider='qwen'"
                ).scalar_one()
            credential = router.credential("qwen")
            status = router.status()
            database.dispose()

        self.assertNotIn("secret-cloud-key", stored)
        self.assertEqual(credential, "secret-cloud-key")
        self.assertTrue(status["api_key_configured"])


if __name__ == "__main__":
    unittest.main()
