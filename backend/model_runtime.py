"""Live local-model activation and residency checks."""
from __future__ import annotations

from datetime import datetime, timezone
import time

import httpx


class ModelRuntimeError(RuntimeError):
    pass


class ModelRuntime:
    def __init__(self, *, timeout_seconds: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @staticmethod
    def _native_ollama_base(base_url: str) -> str:
        value = (base_url or "http://127.0.0.1:11434/v1").rstrip("/")
        return value[:-3] if value.endswith("/v1") else value

    @staticmethod
    def _name_variants(name: str) -> set[str]:
        value = (name or "").strip()
        if not value:
            return set()
        variants = {value}
        if ":" not in value.rsplit("/", 1)[-1]:
            variants.add(f"{value}:latest")
        if value.endswith(":latest"):
            variants.add(value[:-7])
        return variants

    def status(self, selection: dict, base_url: str, api_key: str = "") -> dict:
        mode = selection.get("mode") or selection.get("route")
        provider = selection.get("provider", "")
        model = selection.get("model", "")
        if mode != "local":
            return {
                "required": False,
                "ready": bool(selection.get("ready", True)),
                "state": "not_required",
                "message": "知识库模式无需启动模型" if mode == "knowledge" else "云模型将在查询时调用",
                "provider": provider,
                "model": model,
            }
        if provider != "ollama":
            return self._openai_compatible_status(provider, model, base_url, api_key)
        return self._ollama_status(model, base_url)

    def available_models(self, provider: str, base_url: str) -> dict:
        """List models exposed by a local provider without loading or billing one."""
        provider = (provider or "").strip().lower()
        if provider == "ollama":
            endpoint = f"{self._native_ollama_base(base_url)}/api/tags"
            try:
                with httpx.Client(timeout=min(self.timeout_seconds, 8.0), transport=self.transport) as client:
                    response = client.get(endpoint)
                    response.raise_for_status()
                    rows = response.json().get("models", [])
                names = sorted({item.get("name") or item.get("model") for item in rows if isinstance(item, dict)})
                return {"provider": provider, "reachable": True, "models": [name for name in names if name]}
            except (httpx.HTTPError, ValueError, AttributeError):
                return {"provider": provider, "reachable": False, "models": []}
        if provider == "llamacpp":
            try:
                with httpx.Client(timeout=min(self.timeout_seconds, 8.0), transport=self.transport) as client:
                    response = client.get(f"{base_url.rstrip('/')}/models")
                    response.raise_for_status()
                    rows = response.json().get("data", [])
                names = sorted({item.get("id") for item in rows if isinstance(item, dict)})
                return {"provider": provider, "reachable": True, "models": [name for name in names if name]}
            except (httpx.HTTPError, ValueError, AttributeError):
                return {"provider": provider, "reachable": False, "models": []}
        return {"provider": provider, "reachable": False, "models": []}

    def activate(self, selection: dict, base_url: str, api_key: str = "") -> dict:
        mode = selection.get("mode") or selection.get("route")
        provider = selection.get("provider", "")
        model = selection.get("model", "")
        if mode == "knowledge":
            return self.status(selection, base_url, api_key)
        if mode == "cloud":
            return self._activate_openai_compatible(provider, model, base_url, api_key)
        if provider != "ollama":
            return self._activate_openai_compatible(provider, model, base_url, api_key)
        native = self._native_ollama_base(base_url)
        payload = {
            "model": model,
            "prompt": "只回复 OK",
            "stream": False,
            "keep_alive": "24h",
            "options": {"num_predict": 2, "temperature": 0},
        }
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = client.post(f"{native}/api/generate", json=payload)
                response.raise_for_status()
                body = response.json()
            if not isinstance(body, dict) or body.get("error") or not body.get("done"):
                raise ModelRuntimeError(str(body.get("error") or "Ollama 未完成模型探活"))
        except ModelRuntimeError:
            raise
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise ModelRuntimeError(self._friendly_error(exc, model)) from None
        status = self._ollama_status(model, base_url)
        if not status["ready"]:
            raise ModelRuntimeError(status["message"])
        status.update({
            "verified": True,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": round((time.perf_counter() - started) * 1000),
        })
        return status

    def _ollama_status(self, model: str, base_url: str) -> dict:
        native = self._native_ollama_base(base_url)
        try:
            with httpx.Client(timeout=min(self.timeout_seconds, 8.0), transport=self.transport) as client:
                tags_response = client.get(f"{native}/api/tags")
                tags_response.raise_for_status()
                tags = tags_response.json().get("models", [])
                ps_response = client.get(f"{native}/api/ps")
                ps_response.raise_for_status()
                resident = ps_response.json().get("models", [])
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            return {
                "required": True, "ready": False, "state": "service_unreachable",
                "message": self._friendly_error(exc, model), "provider": "ollama",
                "model": model, "service_reachable": False, "installed": False,
                "loaded": False, "loaded_models": [], "vram_gb": 0,
            }
        installed_names = [item.get("name") or item.get("model") or "" for item in tags]
        loaded_names = [item.get("name") or item.get("model") or "" for item in resident]
        wanted = self._name_variants(model)
        installed = any(wanted & self._name_variants(name) for name in installed_names)
        loaded = any(wanted & self._name_variants(name) for name in loaded_names)
        vram = sum(int(item.get("size_vram") or 0) for item in resident)
        state = "loaded" if loaded else ("not_loaded" if installed else "not_installed")
        message = {
            "loaded": "模型已启动并驻留内存",
            "not_loaded": "模型已安装，但尚未启动",
            "not_installed": "Ollama 已运行，但未安装所选模型",
        }[state]
        return {
            "required": True, "ready": loaded, "state": state, "message": message,
            "provider": "ollama", "model": model, "service_reachable": True,
            "installed": installed, "installed_models": installed_names,
            "loaded": loaded, "loaded_models": loaded_names,
            "vram_gb": round(vram / 1024 ** 3, 2),
        }

    def _openai_compatible_status(self, provider: str, model: str, base_url: str,
                                   api_key: str = "") -> dict:
        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            with httpx.Client(timeout=min(self.timeout_seconds, 8.0), transport=self.transport) as client:
                response = client.get(f"{base_url.rstrip('/')}/models", headers=headers)
                response.raise_for_status()
            return {
                "required": True, "ready": True, "state": "service_ready",
                "message": "模型服务已连接", "provider": provider, "model": model,
                "service_reachable": True, "loaded": None,
            }
        except httpx.HTTPError as exc:
            return {
                "required": True, "ready": False, "state": "service_unreachable",
                "message": self._friendly_error(exc, model), "provider": provider,
                "model": model, "service_reachable": False, "loaded": None,
            }

    def _activate_openai_compatible(self, provider: str, model: str, base_url: str,
                                     api_key: str = "") -> dict:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "只回复 OK"}],
            "max_tokens": 2,
            "temperature": 0,
            "stream": False,
        }
        started = time.perf_counter()
        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = client.post(f"{base_url.rstrip('/')}/chat/completions",
                                       headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
            if not body.get("choices"):
                raise ModelRuntimeError("模型服务返回无效响应")
        except ModelRuntimeError:
            raise
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise ModelRuntimeError(self._friendly_error(exc, model)) from None
        return {
            "required": True, "ready": True, "state": "service_ready",
            "message": "模型已通过真实调用验证", "provider": provider, "model": model,
            "service_reachable": True, "loaded": None, "verified": True,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }

    @staticmethod
    def _friendly_error(exc: Exception, model: str) -> str:
        if isinstance(exc, httpx.ConnectError):
            return "无法连接本地模型服务，请先启动 Ollama 或 llama.cpp 服务"
        if isinstance(exc, httpx.TimeoutException):
            return f"模型 {model} 启动超时，可能正在加载或内存不足"
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                detail = exc.response.json().get("error", "")
            except (ValueError, AttributeError):
                detail = ""
            if isinstance(detail, dict):
                detail = detail.get("message", "")
            return str(detail or f"模型服务返回 HTTP {exc.response.status_code}")[:300]
        return f"模型运行状态检查失败：{type(exc).__name__}"
