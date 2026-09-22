"""Hybrid retrieval orchestration with lexical fallback and optional re-ranking."""
from __future__ import annotations

import logging

from .database import QueryDatabase
from .embeddings import EmbeddingClient, EmbeddingError
from .logging_config import log_event, request_id_context
from .rerank import Reranker


LOGGER = logging.getLogger("docmind.it.retrieval")


class HybridRetriever:
    def __init__(self, database: QueryDatabase, embeddings: EmbeddingClient, *, top_k: int = 5,
                 reranker: Reranker | None = None, rerank_candidate_limit: int = 20):
        self.database = database
        self.embeddings = embeddings
        self.top_k = top_k
        self.reranker = reranker
        self.rerank_candidate_limit = rerank_candidate_limit

    def healthcheck(self) -> tuple[bool, str]:
        return self.embeddings.healthcheck()

    def retrieve(self, question: str, query_id: int | None = None, *, subject_id: str = "legacy",
                 roles=(), groups=(), document_version_id: int | None = None,
                 allow_confidential: bool = False) -> list[dict]:
        """Retrieve for one question.

        ``query_id`` is ``None`` for non-user callers (the evaluation gate) so their embedding
        usage is recorded against ``document_version_id`` instead of a query row.

        ``allow_confidential`` mirrors the caller's clearance. It defaults to ``False``, and a
        ``False`` here can only hide documents: the caller still has to pass the document ACL.
        """
        if not self.database.has_indexed_chunks():
            return []
        request_id = request_id_context.get()
        # When a re-ranker is wired in, fetch a larger candidate set than top_k so the
        # re-ranker has signal to re-order before we trim to the final top_k.
        candidate_limit = (
            max(self.top_k, self.rerank_candidate_limit) if self.reranker else self.top_k
        )
        try:
            result = self.embeddings.embed([question], request_id=request_id)
        except EmbeddingError as exc:
            self.database.record_embedding_usages(
                usages=exc.usages, request_id=request_id, query_id=query_id,
                document_version_id=document_version_id,
            )
            log_event(LOGGER, logging.WARNING, "embedding_query_failed", reason=exc.code)
            candidates = self.database.lexical_search(
                question, candidate_limit, subject_id=subject_id, roles=roles,
                allow_confidential=allow_confidential,
            )
        else:
            self.database.record_embedding_usages(
                usages=result.usages, request_id=request_id, query_id=query_id,
                document_version_id=document_version_id,
            )
            candidates = self.database.hybrid_search(
                question, list(result.vectors[0]), candidate_limit,
                subject_id=subject_id, roles=roles,
                allow_confidential=allow_confidential,
            )
        if self.reranker is not None and candidates:
            candidates = self.reranker.rerank(question, candidates)
        return candidates[:self.top_k]
