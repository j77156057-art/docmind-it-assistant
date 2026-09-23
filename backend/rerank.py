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
    """Offline re-ranker using query/chunk lexical overlap (BM25-ish term weighting).

    Term weight is BM25 IDF over the *candidate set*: a query term that appears in many candidates
    ("the words every chunk repeats") carries almost no weight, while one that appears in a single
    candidate dominates. Frequency inside a chunk only saturates the score, it never raises the
    term's weight — see the note in :meth:`rerank` for why that distinction is load-bearing here.
    """

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

        term_counts: list[dict] = []
        lengths: list[int] = []
        document_frequency: dict = {}
        for candidate in candidates:
            counts: dict = {}
            for term in lexical_terms(_content_of(candidate)):
                counts[term] = counts.get(term, 0) + 1
            term_counts.append(counts)
            lengths.append(sum(counts.values()) or 1)
            for term in set(counts) & query_term_set:
                document_frequency[term] = document_frequency.get(term, 0) + 1

        # IDF counts how many candidates contain the term. The previous version used the term's
        # frequency *inside this candidate* instead, which gave a term a smaller weight the more
        # often the chunk mentioned it — and, worse, treated a word present in nearly every
        # candidate as if it were as informative as a rare one. A chunk repeating the boilerplate
        # ("打印" appeared in 5 of 6 candidates) then outranked the chunk holding the discriminating
        # term ("卡纸", 2 of 6), which is how a section titled 卡纸与耗材 fell out of the top-5
        # behind sections that never mention a paper jam.
        #
        # The length normalisation below still uses the raw term count rather than its ratio to the
        # average candidate length. That deviates from textbook BM25, and it was measured: switching
        # it to length/avg_length changed nothing on the golden set (both variants scored
        # recall@5 0.9524 / citation 0.8571), so only the IDF was corrected here.
        total = len(candidates)
        scored: list[tuple[float, int, dict]] = []
        for index, counts in enumerate(term_counts):
            length = lengths[index]
            score = 0.0
            for term in query_term_set:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                seen_in = document_frequency.get(term, 0)
                inverse = math.log(1 + (total - seen_in + 0.5) / (seen_in + 0.5))
                score += inverse * frequency / (
                    frequency + self.k1 * (1 - self.b + self.b * length)
                )
            scored.append((score, index, candidates[index]))
        scored.sort(key=lambda item: (-item[0], item[1]))

        result: list[dict] = []
        for rank, (score, _index, candidate) in enumerate(scored, 1):
            annotated = dict(candidate)
            annotated["rerank_score"] = round(score, 6)
            annotated["rerank_rank"] = rank
            result.append(annotated)
        return result


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
