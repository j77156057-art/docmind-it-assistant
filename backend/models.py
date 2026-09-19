"""Central model allocation policy for the query application."""
from __future__ import annotations

import os

from .pricing import pricing_status
from .providers import get_provider, model_context_window


class ModelRouter:
    def __init__(self, mode: str | None = None):
        self.mode = (mode or os.getenv("IT_MODEL_MODE") or "knowledge").strip().lower()
        if self.mode not in {"knowledge", "cloud", "local"}:
            raise ValueError(f"不支持的模型模式：{self.mode}")

    def _route(self, route: str, provider_key: str, model_env: str) -> dict:
        provider = get_provider(provider_key)
        model = (os.getenv(model_env) or provider.default_model).strip()
        if not model:
            raise ValueError(f"模型供应商 {provider.key} 未配置模型名称")
        return {
            "route": route,
            "provider": provider.key,
            "model": model,
            "cloud": provider.cloud,
            "context_window": model_context_window(provider.key, model),
            "pricing": pricing_status(provider.key, model),
        }

    def select(self, question: str, evidence: str) -> dict:
        if evidence == "sufficient":
            return self._route("knowledge", "builtin", "IT_BUILTIN_MODEL")
        if self.mode == "cloud":
            return self._route("cloud", os.getenv("IT_CLOUD_PROVIDER", "qwen"), "IT_CLOUD_MODEL")
        if self.mode == "local":
            return self._route("local", os.getenv("IT_LOCAL_PROVIDER", "ollama"), "IT_LOCAL_MODEL")
        return self._route("knowledge", "builtin", "IT_BUILTIN_MODEL")

    def status(self) -> dict:
        if self.mode == "cloud":
            provider_key, model_env = os.getenv("IT_CLOUD_PROVIDER", "qwen"), "IT_CLOUD_MODEL"
        elif self.mode == "local":
            provider_key, model_env = os.getenv("IT_LOCAL_PROVIDER", "ollama"), "IT_LOCAL_MODEL"
        else:
            provider_key, model_env = "builtin", "IT_BUILTIN_MODEL"
        route = self._route(self.mode, provider_key, model_env)
        provider = get_provider(provider_key)
        return {
            "mode": self.mode,
            "provider": route["provider"],
            "model": route["model"],
            "cloud": route["cloud"],
            "context_window": route["context_window"],
            "pricing": route["pricing"],
            "api_key_configured": bool(provider.api_key_env and os.getenv(provider.api_key_env)),
        }
