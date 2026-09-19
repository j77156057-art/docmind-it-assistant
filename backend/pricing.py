"""Token pricing shared by model routing and query accounting.

Prices are CNY per one million tokens. They are defaults only; production
deployments should load an approved price table from configuration storage.
"""
from __future__ import annotations


DEFAULT_PRICING = {
    "qwen": {
        "in": 0.8, "out": 2.0,
        "models": {
            "qwen-plus": {"in": 0.8, "out": 2.0},
            "qwen-turbo": {"in": 0.3, "out": 0.6},
            "qwen-max": {"in": 2.4, "out": 9.6},
        },
    },
    "deepseek": {
        "in": 1.0, "out": 2.0,
        "models": {
            "deepseek-chat": {"in": 1.0, "out": 2.0},
            "deepseek-reasoner": {"in": 4.0, "out": 16.0},
        },
    },
    "ollama": {"in": 0.0, "out": 0.0},
    "llamacpp": {"in": 0.0, "out": 0.0},
    "builtin": {"in": 0.0, "out": 0.0},
}


def price_for(provider: str, model: str = "") -> tuple[float, float]:
    config = DEFAULT_PRICING.get((provider or "").strip().lower(), {})
    models = config.get("models") or {}
    selected = models.get(model) if model else None
    selected = selected if isinstance(selected, dict) else config
    return float(selected.get("in", 0.0)), float(selected.get("out", 0.0))


def cost_cny(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    input_price, output_price = price_for(provider, model)
    cost = (
        max(0, int(prompt_tokens or 0)) * input_price
        + max(0, int(completion_tokens or 0)) * output_price
    ) / 1_000_000
    return round(cost, 8)


def pricing_status(provider: str, model: str) -> dict:
    input_price, output_price = price_for(provider, model)
    return {
        "currency": "CNY",
        "unit_tokens": 1_000_000,
        "input": input_price,
        "output": output_price,
    }
