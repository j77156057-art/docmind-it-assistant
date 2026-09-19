"""Provider catalog migrated from DocMind without development-agent dependencies."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    label: str
    base_url: str
    api_key_env: str
    default_model: str
    cloud: bool
    context_window: int

    def public(self, model: str = "") -> dict:
        data = asdict(self)
        data["model"] = model or self.default_model
        return data


PROVIDERS = {
    "builtin": ProviderSpec("builtin", "内置知识检索", "", "", "deterministic", False, 0),
    "qwen": ProviderSpec(
        "qwen", "通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY", "qwen-plus", True, 131072,
    ),
    "deepseek": ProviderSpec(
        "deepseek", "DeepSeek", "https://api.deepseek.com/v1",
        "DEEPSEEK_API_KEY", "deepseek-chat", True, 131072,
    ),
    "kimi": ProviderSpec(
        "kimi", "Kimi", "https://api.moonshot.cn/v1",
        "MOONSHOT_API_KEY", "kimi-k2-0905-preview", True, 131072,
    ),
    "zhipu": ProviderSpec(
        "zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4",
        "ZHIPU_API_KEY", "glm-4.6", True, 131072,
    ),
    "siliconflow": ProviderSpec(
        "siliconflow", "硅基流动", "https://api.siliconflow.cn/v1",
        "SILICONFLOW_API_KEY", "Qwen/Qwen3-8B", True, 65536,
    ),
    "openai": ProviderSpec(
        "openai", "OpenAI", "https://api.openai.com/v1",
        "OPENAI_API_KEY", "gpt-4o-mini", True, 128000,
    ),
    "ollama": ProviderSpec(
        "ollama", "本地 Ollama", "http://127.0.0.1:11434/v1",
        "", "qwen2.5:7b", False, 32768,
    ),
    "llamacpp": ProviderSpec(
        "llamacpp", "本地 llama.cpp", "http://127.0.0.1:8080/v1",
        "", "qwen3.6-35b-a3b", False, 16384,
    ),
    "custom": ProviderSpec("custom", "自定义 OpenAI 兼容服务", "", "IT_CUSTOM_API_KEY", "", True, 32768),
}


def get_provider(key: str) -> ProviderSpec:
    normalized = (key or "").strip().lower()
    if normalized not in PROVIDERS:
        raise ValueError(f"不支持的模型供应商：{normalized or '空'}")
    return PROVIDERS[normalized]


def model_context_window(provider: str, model: str = "") -> int:
    spec = get_provider(provider)
    name = (model or spec.default_model).lower()
    if provider == "deepseek" and "reasoner" in name:
        return 65536
    if provider == "openai" and any(token in name for token in ("o1", "o3", "gpt-5")):
        return 200000
    return spec.context_window
