"""Re-ranking for hybrid retrieval results.

The hybrid retriever already fuses dense + sparse via Reciprocal Rank Fusion and
returns a small candidate set. A re-ranker re-orders those candidates with a
stronger signal (a cross-encoder style scorer) before the top-k is handed to the
LLM. This is the single highest-leverage accuracy improvement for a production RAG
system and is intentionally pluggable:

* ``LexicalReranker`` is deterministic and offline — it never touches the network.
  It is the safe default and the fallback for every other mode.
* ``ApiReranker`` calls an OpenAI/Cohere-style ``/rerank`` endpoint when one is
  configured. On any failure it degrades to ``LexicalReranker`` rather than
  raising, so a misconfigured re-ranker can never break retrieval.
"""
from __future__ import annotations

import logging
import math

import httpx

from .config import AppSettings
from .text_index import lexical_terms


LOGGER = logging.getLogger("docmind.it.rerank")


class Reranker:
    """Protocol: reorder candidates, optionally annotating ``rerank_score``."""

    def rerank(self, query: str, candidates: list[dict]) -> list[dict]:  # pragma: no cover
        raise NotImplementedError


def _content_of(candidate: dict) -> str:
    return candidate.get("parent_content") or candidate.get("content") or ""


class LexicalReranker(Reranker):
    """Offline re-ranker using query/chunk lexical overlap (BM25-ish term weighting)."""

    def __init__(self, *, k1: float = 1.2, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b

    def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        if not candidates:
            return []
        query_terms = lexical_terms(query)
        if not query_terms:
            return list(candidates)
        query_term_set = set(query_terms)
        scored: list[tuple[float, int, dict]] = []
        for index, candidate in enumerate(candidates):
            text = _content_of(candidate)
            term_counts = {}
            for term in lexical_terms(text):
                term_counts[term] = term_counts.get(term, 0) + 1
            length = sum(term_counts.values()) or 1
            score = 0.0
            for term in query_term_set:
                frequency = term_counts.get(term, 0)
                if not frequency:
                    continue
                inverse = math.log(1 + len(candidates) / (frequency + 0.5))
                score += inverse * frequency / (frequency + self.k1 * (1 - self.b + self.b * length))
            scored.append((score, index, candidate))
        scored.sort(key=lambda item: (-item[0], item[1]))
        for rank, (_score, _index, candidate) in enumerate(scored, 1):
            candidate = dict(candidate)
            candidate["rerank_score"] = round(_score, 6)
            candidate["rerank_rank"] = rank
            scored[rank - 1] = (_score, _index, candidate)
        return [item[2] for item in scored]


class ApiReranker(Reranker):
    """Cross-encoder style re-ranker backed by an external ``/rerank`` endpoint."""

    def __init__(self, *, base_url: str, model: str, api_key: str = "",
                 timeout_seconds: float = 30.0, fallback: Reranker | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.fallback = fallback or LexicalReranker()

    def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        if not candidates:
            return []
        documents = [_content_of(candidate) for candidate in candidates]
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{self.base_url}/rerank",
                    headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                    json={"model": self.model, "query": query, "documents": documents},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.warning("rerank_api_failed", extra={"reason": type(exc).__name__})
            return self.fallback.rerank(query, candidates)
        ranked = {item["index"]: item.get("relevance_score", 0.0)
                  for item in payload.get("results", [])}
        if not ranked:
            return self.fallback.rerank(query, candidates)
        ordered = sorted(
            range(len(candidates)),
            key=lambda i: -float(ranked.get(i, float("-inf"))),
        )
        result: list[dict] = []
        for rank, index in enumerate(ordered, 1):
            candidate = dict(candidates[index])
            candidate["rerank_score"] = round(float(ranked.get(index, 0.0)), 6)
            candidate["rerank_rank"] = rank
            result.append(candidate)
        return result


def build_reranker(settings: AppSettings) -> Reranker | None:
    """Return ``None`` when re-ranking is disabled, else the configured re-ranker."""
    if not settings.rerank_enabled:
        return None
    if settings.rerank_mode == "api" and settings.rerank_base_url:
        return ApiReranker(
            base_url=settings.rerank_base_url,
            model=settings.rerank_model,
            api_key=settings.rerank_api_key.get_secret_value() if settings.rerank_api_key else "",
        )
    return LexicalReranker()
