"""Versioned document import pipeline, separate from the query application."""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
import uuid

from backend import GovernanceError, QueryDatabase
from backend.embeddings import EmbeddingClient, EmbeddingError

from .chunker import chunk_document
from .parsers import parse_document


def embedding_retryable(code: str) -> bool:
    """Only transient provider conditions are worth another attempt."""
    if code in {"embedding_timeout", "embedding_unavailable"}:
        return True
    if code.startswith("embedding_http_"):
        try:
            status = int(code.rsplit("_", 1)[1])
        except ValueError:
            return False
        return status == 429 or status >= 500
    return False


class DocumentProcessingError(RuntimeError):
    """Failure while indexing a version; ``retryable`` drives the queue's retry decision."""

    def __init__(self, code: str, *, detail: str = "", retryable: bool = False):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.retryable = retryable


class DocumentIngestionService:
    def __init__(self, database: QueryDatabase, embeddings: EmbeddingClient, *,
                 max_bytes: int, chunk_max_chars: int, chunk_overlap_chars: int,
                 chunk_child_max_chars: int = 400,
                 max_characters: int = 2_000_000, max_pages: int = 500,
                 require_review: bool = False):
        self.database = database
        self.embeddings = embeddings
        self.max_bytes = max_bytes
        self.chunk_max_chars = chunk_max_chars
        self.chunk_overlap_chars = chunk_overlap_chars
        self.chunk_child_max_chars = chunk_child_max_chars
        self.max_characters = max_characters
        self.max_pages = max_pages
        # Governance mode "review": indexing stops at `staged` and waits for a human decision.
        self.require_review = require_review

    def import_file(self, path: str | Path, *, title: str = "", source_key: str = "",
                    access_scope: str | None = None,
                    classification: str | None = None,
                    submitted_by_subject_id: str = "",
                    request_id: str = "") -> dict:
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
            child_max_chars=self.chunk_child_max_chars,
        )
        version = self.database.begin_document_import(
            source_key=(source_key.strip() or file_path.as_uri()),
            title=parsed.title,
            mime_type=parsed.mime_type,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            access_scope=access_scope,
            classification=classification,
            submitted_by_subject_id=submitted_by_subject_id,
            request_id=request_id,
        )
        if version["duplicate"]:
            return {**version, "title": parsed.title, "chunk_count": len(chunks)}
        embed_request_id = f"ingest-{uuid.uuid4().hex}"
        try:
            result = self.embeddings.embed([
                f"{chunk.heading}\n{chunk.content}".strip() for chunk in chunks
            ], request_id=embed_request_id)
            self.database.record_embedding_usages(
                usages=result.usages,
                request_id=embed_request_id,
                document_version_id=version["version_id"],
            )
            state = self.database.finalize_document_indexing(
                version["version_id"], chunks, result.vectors,
                publish=not self.require_review,
                actor_subject_id=submitted_by_subject_id,
                request_id=request_id,
            )
        except EmbeddingError as exc:
            self.database.record_embedding_usages(
                usages=exc.usages,
                request_id=embed_request_id,
                document_version_id=version["version_id"],
            )
            self.database.fail_document_import(version["version_id"], exc.code)
            raise
        except Exception:
            self.database.fail_document_import(version["version_id"], "import_failed")
            raise
        return {
            **version,
            "status": state["status"],
            "title": parsed.title,
            "chunk_count": len(chunks),
            "embedding_mode": self.embeddings.mode,
        }

    def register_version(self, path: str | Path, *, title: str = "", source_key: str = "",
                         mime_type: str = "", access_scope: str | None = None,
                         classification: str | None = None,
                         submitted_by_subject_id: str = "",
                         request_id: str = "") -> dict:
        """Create (or reopen) the version row without parsing anything.

        The asynchronous path uses this so the HTTP request returns as soon as the upload is
        durable; parsing, chunking and embedding happen in the worker.
        """
        file_path = Path(path).resolve()
        if not file_path.is_file():
            raise ValueError("文档不存在或不是文件")
        if file_path.stat().st_size > self.max_bytes:
            raise ValueError("文档超过允许的大小")
        raw = file_path.read_bytes()
        if len(raw) > self.max_bytes:
            raise ValueError("文档超过允许的大小")
        normalized_title = title.strip() or file_path.stem
        guessed_mime = mime_type or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        result = self.database.begin_document_import(
            source_key=(source_key.strip() or file_path.as_uri()),
            title=normalized_title,
            mime_type=guessed_mime,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            access_scope=access_scope,
            classification=classification,
            submitted_by_subject_id=submitted_by_subject_id,
            request_id=request_id,
        )
        return {**result, "title": normalized_title}

    def parse_and_chunk(self, source_path: str | Path, *, fallback_title: str = ""):
        """Parse and chunk a stored original. Shared by the synchronous and graph paths.

        ``fallback_title`` is the title the import boundary already resolved (the operator's value,
        else the uploaded filename). The parser uses it before falling back to the stored file name.
        Not passing it is exactly why an asynchronously indexed document used to lose the title the
        operator typed and come back as ``v1-<filename>``: the synchronous ``import_file`` path has
        always passed it, so the two paths disagreed.
        """
        try:
            parsed = parse_document(
                Path(source_path), title=fallback_title, max_bytes=self.max_bytes,
                max_characters=self.max_characters, max_pages=self.max_pages,
            )
            chunks = chunk_document(
                parsed, max_chars=self.chunk_max_chars, overlap_chars=self.chunk_overlap_chars,
                child_max_chars=self.chunk_child_max_chars,
            )
        except Exception as exc:  # noqa: BLE001 - parsers raise library-specific errors
            # A corrupt or unsupported file is deterministic: never spend the retry budget on it.
            # Only the exception type is recorded so no document content reaches the logs.
            raise DocumentProcessingError(
                "parse_failed", detail=type(exc).__name__, retryable=False,
            ) from None
        return parsed, chunks

    def embed_texts(self, texts, *, request_id: str, document_version_id: int):
        """Embed one batch and always record provider usage, including on failure."""
        try:
            result = self.embeddings.embed(list(texts), request_id=request_id)
        except EmbeddingError as exc:
            self.database.record_embedding_usages(
                usages=exc.usages,
                request_id=request_id,
                document_version_id=document_version_id,
            )
            raise DocumentProcessingError(
                exc.code, detail=str(exc), retryable=embedding_retryable(exc.code),
            ) from None
        self.database.record_embedding_usages(
            usages=result.usages,
            request_id=request_id,
            document_version_id=document_version_id,
        )
        return result

    def process_version(self, *, document_id: int, version_id: int, source_path: str | Path,
                        publish: bool, actor_subject_id: str = "",
                        request_id: str = "") -> dict:
        """Parse, chunk and embed an already-registered version in one call.

        Raises ``DocumentProcessingError`` with an explicit retryability flag: content and
        configuration problems must not burn the retry budget.
        """
        parsed, chunks = self.parse_and_chunk(
            source_path, fallback_title=self.database.document_title(document_id),
        )
        try:
            self.database.mark_document_version_processing(
                version_id, title=parsed.title, mime_type=parsed.mime_type,
            )
        except GovernanceError as exc:
            raise DocumentProcessingError(exc.code, detail=str(exc), retryable=False) from None
        embed_request_id = f"ingest-{uuid.uuid4().hex}"
        result = self.embed_texts(
            [f"{chunk.heading}\n{chunk.content}".strip() for chunk in chunks],
            request_id=embed_request_id, document_version_id=version_id,
        )
        try:
            state = self.database.finalize_document_indexing(
                version_id, chunks, result.vectors, publish=publish,
                actor_subject_id=actor_subject_id, request_id=request_id,
            )
        except GovernanceError as exc:
            raise DocumentProcessingError(exc.code, detail=str(exc), retryable=False) from None
        return {
            "document_id": document_id,
            "version_id": version_id,
            "status": state["status"],
            "title": parsed.title,
            "chunk_count": len(chunks),
            "embedding_mode": self.embeddings.mode,
        }
