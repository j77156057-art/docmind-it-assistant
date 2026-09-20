"""Versioned document import pipeline, separate from the query application."""
from __future__ import annotations

import hashlib
from pathlib import Path
import uuid

from backend import QueryDatabase
from backend.embeddings import EmbeddingClient, EmbeddingError

from .chunker import chunk_document
from .parsers import parse_document


class DocumentIngestionService:
    def __init__(self, database: QueryDatabase, embeddings: EmbeddingClient, *,
                 max_bytes: int, chunk_max_chars: int, chunk_overlap_chars: int,
                 max_characters: int = 2_000_000, max_pages: int = 500):
        self.database = database
        self.embeddings = embeddings
        self.max_bytes = max_bytes
        self.chunk_max_chars = chunk_max_chars
        self.chunk_overlap_chars = chunk_overlap_chars
        self.max_characters = max_characters
        self.max_pages = max_pages

    def import_file(self, path: str | Path, *, title: str = "", source_key: str = "",
                    access_scope: str | None = None,
                    classification: str | None = None) -> dict:
        file_path = Path(path).resolve()
        if not file_path.is_file():
            raise ValueError("文档不存在或不是文件")
        if file_path.stat().st_size > self.max_bytes:
            raise ValueError("文档超过允许的大小")
        raw = file_path.read_bytes()
        if len(raw) > self.max_bytes:
            raise ValueError("文档超过允许的大小")
        parsed = parse_document(
            file_path, title=title, max_bytes=self.max_bytes,
            max_characters=self.max_characters, max_pages=self.max_pages,
        )
        chunks = chunk_document(
            parsed, max_chars=self.chunk_max_chars, overlap_chars=self.chunk_overlap_chars,
        )
        version = self.database.begin_document_import(
            source_key=(source_key.strip() or file_path.as_uri()),
            title=parsed.title,
            mime_type=parsed.mime_type,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            access_scope=access_scope,
            classification=classification,
        )
        if version["duplicate"]:
            return {**version, "title": parsed.title, "chunk_count": len(chunks)}
        request_id = f"ingest-{uuid.uuid4().hex}"
        try:
            result = self.embeddings.embed([
                f"{chunk.heading}\n{chunk.content}".strip() for chunk in chunks
            ], request_id=request_id)
            self.database.record_embedding_usages(
                usages=result.usages,
                request_id=request_id,
                document_version_id=version["version_id"],
            )
            self.database.complete_document_import(version["version_id"], chunks, result.vectors)
        except EmbeddingError as exc:
            self.database.record_embedding_usages(
                usages=exc.usages,
                request_id=request_id,
                document_version_id=version["version_id"],
            )
            self.database.fail_document_import(version["version_id"], exc.code)
            raise
        except Exception:
            self.database.fail_document_import(version["version_id"], "import_failed")
            raise
        return {
            **version,
            "status": "indexed",
            "title": parsed.title,
            "chunk_count": len(chunks),
            "embedding_mode": self.embeddings.mode,
        }
