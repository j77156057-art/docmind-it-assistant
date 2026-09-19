"""Deterministic development embeddings and real OpenAI-compatible embedding client."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import time

import httpx

from .providers import get_provider
from .text_index import lexical_terms


@dataclass(frozen=True)
class EmbeddingUsage:
    provider: str
    model: str
    prompt_tokens: int | None
    total_tokens: int | None
    usage_reported: bool
    provider_request_id: str
    latency_ms: int
    status: str = "succeeded"
    error_code: str | None = None


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    usages: tuple[EmbeddingUsage, ...]


class EmbeddingError(RuntimeError):
    def __init__(self, code: str, usages: list[EmbeddingUsage] | None = None):
        super().__init__(code)
        self.code = code
        self.usages = tuple(usages or [])


class EmbeddingClient:
    def __init__(self, *, mode: str, provider: str, model: str, base_url: str,
                 api_key: str, dimension: int = 1024, timeout_seconds: float = 60.0,
                 batch_size: int = 16, transport: httpx.BaseTransport | None = None):
        self.mode = mode
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dimension = dimension
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size
        self.transport = transport

    def embed(self, texts: list[str], *, request_id: str = "") -> EmbeddingResult:
        if not texts:
            return EmbeddingResult((), ())
        if self.mode == "hash":
            return EmbeddingResult(tuple(self._hash_vector(text) for text in texts), ())
        if not self.base_url.startswith(("http://", "https://")):
            raise EmbeddingError("embedding_base_url_invalid")
        if not self.api_key:
            raise EmbeddingError("embedding_api_key_missing")

        vectors: list[tuple[float, ...]] = []
        usages: list[EmbeddingUsage] = []
        endpoint = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if request_id:
            headers["X-Request-ID"] = request_id[:128]
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            for offset in range(0, len(texts), self.batch_size):
                batch = texts[offset:offset + self.batch_size]
                started = time.perf_counter()
                try:
                    response = client.post(endpoint, headers=headers, json={
                        "model": self.model,
                        "input": batch,
                        "dimensions": self.dimension,
                    })
                except httpx.TransportError as exc:
                    latency = round((time.perf_counter() - started) * 1000)
                    code = "embedding_timeout" if isinstance(exc, httpx.TimeoutException) else "embedding_unavailable"
                    usages.append(EmbeddingUsage(
                        self.provider, self.model, None, None, False, "", latency, "failed", code,
                    ))
                    raise EmbeddingError(code, usages) from None
                latency = round((time.perf_counter() - started) * 1000)
                provider_request_id = response.headers.get("x-request-id", "")[:128]
                if response.status_code >= 400:
                    code = f"embedding_http_{response.status_code}"
                    usages.append(EmbeddingUsage(
                        self.provider, self.model, None, None, False,
                        provider_request_id, latency, "failed", code,
                    ))
                    raise EmbeddingError(code, usages)
                try:
                    body = response.json()
                    data = sorted(body["data"], key=lambda item: item["index"])
                    batch_vectors = [tuple(float(value) for value in item["embedding"]) for item in data]
                    if len(batch_vectors) != len(batch) or any(
                        len(vector) != self.dimension for vector in batch_vectors
                    ):
                        raise ValueError("invalid dimension")
                except (KeyError, TypeError, ValueError):
                    code = "embedding_response_invalid"
                    usages.append(EmbeddingUsage(
                        self.provider, self.model, None, None, False,
                        provider_request_id, latency, "failed", code,
                    ))
                    raise EmbeddingError(code, usages) from None
                usage = body.get("usage") if isinstance(body, dict) else {}
                usage = usage if isinstance(usage, dict) else {}
                prompt_tokens = _token_value(usage.get("prompt_tokens"))
                total_tokens = _token_value(usage.get("total_tokens"))
                usages.append(EmbeddingUsage(
                    self.provider, self.model, prompt_tokens, total_tokens,
                    prompt_tokens is not None, provider_request_id, latency,
                ))
                vectors.extend(batch_vectors)
        return EmbeddingResult(tuple(vectors), tuple(usages))

    def _hash_vector(self, text: str) -> tuple[float, ...]:
        values = [0.0] * self.dimension
        for term in lexical_terms(text):
            digest = hashlib.sha256(term.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            values[index] += -1.0 if digest[4] & 1 else 1.0
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return tuple(value / norm for value in values)

    def healthcheck(self) -> tuple[bool, str]:
        if self.mode == "hash":
            return True, "ok"
        if not self.base_url.startswith(("http://", "https://")):
            return False, "embedding_base_url_invalid"
        if not self.api_key:
            return False, "embedding_api_key_missing"
        return True, "ok"


def build_embedding_client(settings, *, transport: httpx.BaseTransport | None = None) -> EmbeddingClient:
    provider = get_provider(settings.embedding_provider)
    if settings.embedding_provider == "custom":
        default_url = settings.custom_base_url
    else:
        default_url = provider.base_url
    return EmbeddingClient(
        mode=settings.embedding_mode,
        provider=provider.key if settings.embedding_mode == "provider" else "builtin",
        model=settings.embedding_model if settings.embedding_mode == "provider" else "hash-1024",
        base_url=settings.embedding_base_url or default_url,
        api_key=settings.credential_value(provider.api_key_env),
        dimension=settings.embedding_dimension,
        timeout_seconds=settings.embedding_timeout_seconds,
        batch_size=settings.embedding_batch_size,
        transport=transport,
    )
def _token_value(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None
