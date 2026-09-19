import os
import unittest
from unittest.mock import patch

from backend import ModelRouter, cost_cny, get_provider, model_context_window, price_for


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


if __name__ == "__main__":
    unittest.main()
