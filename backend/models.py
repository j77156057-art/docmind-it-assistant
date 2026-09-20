"""Central model allocation policy for the query application."""
from __future__ import annotations

from collections.abc import Callable
import os

from .pricing import pricing_status
from .providers import PROVIDERS, get_provider, model_context_window


class ModelRouter:
    def __init__(self, mode: str | None = None, *, local_provider: str | None = None,
                 local_model: str | None = None, cloud_provider: str | None = None,
                 cloud_model: str | None = None, builtin_model: str | None = None,
                 configured_credentials: frozenset[str] | None = None,
                 credentials: dict[str, str] | None = None,
                 cloud_base_url: str = "", local_base_url: str = "", custom_base_url: str = "",
                 runtime_loader: Callable[[], dict | None] | None = None):
        self.mode = (mode or os.getenv("IT_MODEL_MODE") or "knowledge").strip().lower()
        if self.mode not in {"knowledge", "cloud", "local"}:
            raise ValueError(f"不支持的模型模式：{self.mode}")
        self.local_provider = (local_provider or os.getenv("IT_LOCAL_PROVIDER") or "ollama").strip().lower()
        self.local_model = (local_model if local_model is not None else os.getenv("IT_LOCAL_MODEL", "")).strip()
        self.cloud_provider = (cloud_provider or os.getenv("IT_CLOUD_PROVIDER") or "qwen").strip().lower()
        self.cloud_model = (cloud_model if cloud_model is not None else os.getenv("IT_CLOUD_MODEL", "")).strip()
        self.builtin_model = (builtin_model or os.getenv("IT_BUILTIN_MODEL") or "deterministic").strip()
        self.configured_credentials = configured_credentials
        self.credentials = credentials or {}
        self.cloud_base_url = cloud_base_url.strip()
        self.local_base_url = local_base_url.strip()
        self.custom_base_url = custom_base_url.strip()
        self.runtime_loader = runtime_loader

    @classmethod
    def from_settings(cls, settings, *, runtime_loader=None) -> "ModelRouter":
        return cls(
            settings.model_mode,
            local_provider=settings.local_provider,
            local_model=settings.local_model,
            cloud_provider=settings.cloud_provider,
            cloud_model=settings.cloud_model,
            builtin_model=settings.builtin_model,
            configured_credentials=settings.configured_credentials,
            credentials={
                name: settings.credential_value(name)
                for name in settings.configured_credentials
            },
            cloud_base_url=settings.cloud_base_url,
            local_base_url=settings.local_base_url,
            custom_base_url=settings.custom_base_url,
            runtime_loader=runtime_loader,
        )

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

    def _selection(self, mode: str, provider_key: str = "", model: str = "") -> dict:
        normalized_mode = (mode or "").strip().lower()
        if normalized_mode == "knowledge":
            return self._route("knowledge", "builtin", model or self.builtin_model)
        if normalized_mode == "cloud":
            provider_key = (provider_key or self.cloud_provider).strip().lower()
            provider = get_provider(provider_key)
            if not provider.cloud:
                raise ValueError("云模型模式必须选择云端供应商")
            return self._route("cloud", provider_key, model or self.cloud_model)
        if normalized_mode == "local":
            provider_key = (provider_key or self.local_provider).strip().lower()
            provider = get_provider(provider_key)
            if provider.cloud or provider.key == "builtin":
                raise ValueError("本地模型模式必须选择本地供应商")
            return self._route("local", provider_key, model or self.local_model)
        raise ValueError(f"不支持的模型模式：{normalized_mode or '空'}")

    def _configured_selection(self) -> dict:
        override = self.runtime_loader() if self.runtime_loader else None
        if override:
            return self._selection(
                str(override.get("mode", "")),
                str(override.get("provider", "")),
                str(override.get("model", "")),
            )
        if self.mode == "cloud":
            return self._selection("cloud", self.cloud_provider, self.cloud_model)
        if self.mode == "local":
            return self._selection("local", self.local_provider, self.local_model)
        return self._selection("knowledge", "builtin", self.builtin_model)

    def _credential_is_configured(self, provider_key: str) -> bool:
        provider = get_provider(provider_key)
        return bool(
            provider.api_key_env
            and (
                provider.api_key_env in self.configured_credentials
                if self.configured_credentials is not None
                else os.getenv(provider.api_key_env)
            )
        )

    def selection_status(self, mode: str, provider: str = "", model: str = "") -> dict:
        route = self._selection(mode, provider, model)
        spec = get_provider(route["provider"])
        api_key_configured = self._credential_is_configured(spec.key)
        ready, reason = True, "ok"
        if spec.cloud and not api_key_configured:
            ready, reason = False, "model_api_key_missing"
        if spec.key == "custom" and not self.custom_base_url:
            ready, reason = False, "model_base_url_missing"
        return {
            "mode": route["route"],
            "provider": route["provider"],
            "model": route["model"],
            "cloud": route["cloud"],
            "context_window": route["context_window"],
            "pricing": route["pricing"],
            "api_key_configured": api_key_configured,
            "ready": ready,
            "reason": reason,
        }

    def validate_selection(self, mode: str, provider: str = "", model: str = "") -> dict:
        status = self.selection_status(mode, provider, model)
        if not status["ready"]:
            if status["reason"] == "model_api_key_missing":
                raise ValueError("所选供应商尚未配置 API Key")
            raise ValueError("所选供应商尚未配置服务地址")
        return status

    def catalog(self) -> list[dict]:
        items = []
        for provider in PROVIDERS.values():
            mode = "knowledge" if provider.key == "builtin" else ("cloud" if provider.cloud else "local")
            items.append({
                "key": provider.key,
                "label": provider.label,
                "mode": mode,
                "default_model": provider.default_model,
                "context_window": provider.context_window,
                "api_key_configured": self._credential_is_configured(provider.key),
                "base_url_configured": provider.key != "custom" or bool(self.custom_base_url),
            })
        return items

    def select(self, question: str, evidence: str) -> dict:
        if evidence == "sufficient":
            return self._route("knowledge", "builtin", self.builtin_model)
        return self._configured_selection()

    def status(self) -> dict:
        selection = self._configured_selection()
        return self.selection_status(
            selection["route"], selection["provider"], selection["model"],
        )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            status = self.status()
        except ValueError:
            return False, "invalid_model_configuration"
        return status["ready"], status["reason"]

    def credential(self, provider_key: str) -> str:
        provider = get_provider(provider_key)
        return (
            self.credentials.get(provider.api_key_env, "") or os.getenv(provider.api_key_env, "")
            if provider.api_key_env else ""
        )

    def base_url(self, route: dict) -> str:
        provider = get_provider(route["provider"])
        if provider.key == "custom":
            return self.custom_base_url
        if route["route"] == "cloud" and self.cloud_base_url:
            return self.cloud_base_url
        if route["route"] == "local" and self.local_base_url:
            return self.local_base_url
        return provider.base_url
