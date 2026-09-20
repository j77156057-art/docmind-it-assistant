"""OpenAI-compatible model gateway with bounded retries and authoritative usage capture."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import time

import httpx

from .pricing import cost_cny, pricing_status


RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class GatewayAttempt:
    attempt: int
    status: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    usage_reported: bool
    provider_request_id: str
    latency_ms: int
    error_code: str | None = None


@dataclass(frozen=True)
class GatewayResult:
    content: str
    attempts: tuple[GatewayAttempt, ...]


class ModelGatewayError(RuntimeError):
    def __init__(self, code: str, attempts: list[GatewayAttempt]):
        super().__init__(code)
        self.code = code
        self.attempts = tuple(attempts)


class ModelGateway:
    def __init__(self, *, timeout_seconds: float = 30.0, max_retries: int = 1,
                 retry_backoff_seconds: float = 0.25,
                 max_output_tokens: int = 512, temperature: float = 0.1,
                 transport: httpx.BaseTransport | None = None):
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.transport = transport

    def complete(self, *, route: dict, base_url: str, api_key: str, question: str,
                 context: str = "", citations: list[dict] | None = None,
                 request_id: str = "") -> GatewayResult:
        if not base_url.startswith(("http://", "https://")):
            raise ModelGatewayError("model_base_url_invalid", [])
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if request_id:
            headers["X-Request-ID"] = request_id[:128]
        citation_hint = "\n".join(
            f"[{index}] {item.get('source', '内部资料')} / {item.get('section', '')}"
            for index, item in enumerate(citations or [], 1)
        )
        evidence_block = context.strip() or "（没有检索到可供引用的内部资料）"
        is_ollama = route.get("provider") == "ollama"
        payload = {
            "model": route["model"],
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是企业 IT 客服助手，只能基于提供的内部资料回答。"
                        "不要执行命令或修改系统；资料不足时必须明确说明资料不足。"
                        "涉及密码、权限、网络配置等高风险操作时，提醒用户先核实。"
                        "回答要简洁、分步骤，并保留资料引用标记。"
                    ),
                },
                {"role": "user", "content": (
                    f"用户问题：\n{question}\n\n内部知识库资料：\n{evidence_block}"
                    f"\n\n资料引用：\n{citation_hint or '（无）'}"
                )},
            ],
            "temperature": self.temperature,
            "stream": False,
        }
        if is_ollama:
            # Ollama's native API supports disabling Qwen thinking; its
            # OpenAI-compatible endpoint may return an empty content field
            # while spending the whole budget on reasoning.
            payload["think"] = False
            payload["options"] = {"num_predict": self.max_output_tokens}
        else:
            payload["max_tokens"] = self.max_output_tokens
        attempts: list[GatewayAttempt] = []
        if is_ollama:
            native_base = base_url.rstrip("/")
            if native_base.endswith("/v1"):
                native_base = native_base[:-3]
            endpoint = f"{native_base}/api/chat"
        else:
            endpoint = f"{base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            for attempt_number in range(1, self.max_retries + 2):
                started = time.perf_counter()
                try:
                    response = client.post(endpoint, headers=headers, json=payload)
                    latency_ms = round((time.perf_counter() - started) * 1000)
                    provider_request_id = response.headers.get("x-request-id", "")[:128]
                    if response.status_code >= 400:
                        error_code = f"provider_http_{response.status_code}"
                        attempts.append(GatewayAttempt(
                            attempt_number, "failed", None, None, None, False,
                            provider_request_id, latency_ms, error_code,
                        ))
                        if response.status_code in RETRYABLE_STATUS_CODES and attempt_number <= self.max_retries:
                            self._backoff(attempt_number, response.headers.get("retry-after"))
                            continue
                        raise ModelGatewayError(error_code, attempts)
                    try:
                        body = response.json()
                        if is_ollama:
                            content = body["message"]["content"]
                        else:
                            content = body["choices"][0]["message"]["content"]
                        if not isinstance(content, str) or not content.strip():
                            raise ValueError("empty content")
                    except (KeyError, IndexError, TypeError, ValueError):
                        error_code = "provider_response_invalid"
                        attempts.append(GatewayAttempt(
                            attempt_number, "failed", None, None, None, False,
                            provider_request_id, latency_ms, error_code,
                        ))
                        raise ModelGatewayError(error_code, attempts) from None
                    usage = body.get("usage") if isinstance(body, dict) else None
                    usage = usage if isinstance(usage, dict) else {}
                    prompt_tokens = _token_value(
                        usage.get("prompt_tokens") if not is_ollama
                        else body.get("prompt_eval_count")
                    )
                    completion_tokens = _token_value(
                        usage.get("completion_tokens") if not is_ollama
                        else body.get("eval_count")
                    )
                    if is_ollama:
                        native_prompt = _token_value(body.get("prompt_eval_count"))
                        native_completion = _token_value(body.get("eval_count"))
                        total_tokens = (
                            native_prompt + native_completion
                            if native_prompt is not None and native_completion is not None else None
                        )
                    else:
                        total_tokens = _token_value(usage.get("total_tokens"))
                    usage_reported = prompt_tokens is not None and completion_tokens is not None
                    if total_tokens is None and usage_reported:
                        total_tokens = prompt_tokens + completion_tokens
                    attempts.append(GatewayAttempt(
                        attempt_number, "succeeded", prompt_tokens, completion_tokens,
                        total_tokens, usage_reported, provider_request_id, latency_ms,
                    ))
                    return GatewayResult(content.strip(), tuple(attempts))
                except ModelGatewayError:
                    raise
                except httpx.TransportError as exc:
                    latency_ms = round((time.perf_counter() - started) * 1000)
                    error_code = "provider_timeout" if isinstance(exc, httpx.TimeoutException) else "provider_unavailable"
                    attempts.append(GatewayAttempt(
                        attempt_number, "failed", None, None, None, False, "", latency_ms, error_code,
                    ))
                    if attempt_number <= self.max_retries:
                        self._backoff(attempt_number)
                        continue
                    raise ModelGatewayError(error_code, attempts) from None
        raise ModelGatewayError("provider_unavailable", attempts)

    def _backoff(self, attempt_number: int, retry_after: str | None = None) -> None:
        delay = self.retry_backoff_seconds * (2 ** (attempt_number - 1))
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(min(delay, 2.0))

    @staticmethod
    def public_usage(route: dict, attempt: GatewayAttempt) -> dict:
        pricing = pricing_status(route["provider"], route["model"])
        charge = None
        if attempt.usage_reported and pricing["known"]:
            charge = cost_cny(
                route["provider"], route["model"],
                attempt.prompt_tokens or 0, attempt.completion_tokens or 0,
            )
        return {
            **asdict(attempt),
            "provider_request_id": bool(attempt.provider_request_id),
            "cost_cny": charge,
            "pricing_known": pricing["known"],
        }


def _token_value(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)
