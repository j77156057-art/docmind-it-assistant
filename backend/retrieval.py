"""Hybrid retrieval orchestration with lexical fallback."""
from __future__ import annotations

import logging

from .database import QueryDatabase
from .embeddings import EmbeddingClient, EmbeddingError
from .logging_config import log_event, request_id_context


LOGGER = logging.getLogger("docmind.it.retrieval")


class HybridRetriever:
    def __init__(self, database: QueryDatabase, embeddings: EmbeddingClient, *, top_k: int = 5):
        self.database = database
        self.embeddings = embeddings
        self.top_k = top_k

    def healthcheck(self) -> tuple[bool, str]:
        return self.embeddings.healthcheck()

    def retrieve(self, question: str, query_id: int, *, subject_id: str = "legacy",
                 roles=(), groups=()) -> list[dict]:
        if not self.database.has_indexed_chunks():
            return []
        request_id = request_id_context.get()
        try:
            result = self.embeddings.embed([question], request_id=request_id)
        except EmbeddingError as exc:
            self.database.record_embedding_usages(
                usages=exc.usages, request_id=request_id, query_id=query_id,
            )
            log_event(LOGGER, logging.WARNING, "embedding_query_failed", reason=exc.code)
            return self.database.lexical_search(
                question, self.top_k, subject_id=subject_id, roles=roles, groups=groups,
            )
        self.database.record_embedding_usages(
            usages=result.usages, request_id=request_id, query_id=query_id,
        )
        return self.database.hybrid_search(
            question, list(result.vectors[0]), self.top_k,
            subject_id=subject_id, roles=roles, groups=groups,
        )
