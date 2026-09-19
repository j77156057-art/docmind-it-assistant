"""Central model allocation policy for the query application."""
from __future__ import annotations

import os

from .pricing import pricing_status
from .providers import get_provider, model_context_window


class ModelRouter:
    def __init__(self, mode: str | None = None, *, local_provider: str | None = None,
                 local_model: str | None = None, cloud_provider: str | None = None,
                 cloud_model: str | None = None, builtin_model: str | None = None,
                 configured_credentials: frozenset[str] | None = None):
        self.mode = (mode or os.getenv("IT_MODEL_MODE") or "knowledge").strip().lower()
        if self.mode not in {"knowledge", "cloud", "local"}:
            raise ValueError(f"不支持的模型模式：{self.mode}")
        self.local_provider = (local_provider or os.getenv("IT_LOCAL_PROVIDER") or "ollama").strip().lower()
        self.local_model = (local_model if local_model is not None else os.getenv("IT_LOCAL_MODEL", "")).strip()
        self.cloud_provider = (cloud_provider or os.getenv("IT_CLOUD_PROVIDER") or "qwen").strip().lower()
        self.cloud_model = (cloud_model if cloud_model is not None else os.getenv("IT_CLOUD_MODEL", "")).strip()
        self.builtin_model = (builtin_model or os.getenv("IT_BUILTIN_MODEL") or "deterministic").strip()
        self.configured_credentials = configured_credentials

    def _route(self, route: str, provider_key: str, configured_model: str) -> dict:
        provider = get_provider(provider_key)
        model = (configured_model or provider.default_model).strip()
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
            return self._route("knowledge", "builtin", self.builtin_model)
        if self.mode == "cloud":
            return self._route("cloud", self.cloud_provider, self.cloud_model)
        if self.mode == "local":
            return self._route("local", self.local_provider, self.local_model)
        return self._route("knowledge", "builtin", self.builtin_model)

    def status(self) -> dict:
        if self.mode == "cloud":
            provider_key, model = self.cloud_provider, self.cloud_model
        elif self.mode == "local":
            provider_key, model = self.local_provider, self.local_model
        else:
            provider_key, model = "builtin", self.builtin_model
        route = self._route(self.mode, provider_key, model)
        provider = get_provider(provider_key)
        return {
            "mode": self.mode,
            "provider": route["provider"],
            "model": route["model"],
            "cloud": route["cloud"],
            "context_window": route["context_window"],
            "pricing": route["pricing"],
            "api_key_configured": bool(
                provider.api_key_env
                and (
                    provider.api_key_env in self.configured_credentials
                    if self.configured_credentials is not None
                    else os.getenv(provider.api_key_env)
                )
            ),
        }

    def healthcheck(self) -> tuple[bool, str]:
        try:
            status = self.status()
        except ValueError:
            return False, "invalid_model_configuration"
        if status["cloud"] and not status["api_key_configured"]:
            return False, "model_api_key_missing"
        return True, "ok"
