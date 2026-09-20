"""SQLAlchemy query repository supporting PostgreSQL and isolated SQLite tests."""
from __future__ import annotations

from pathlib import Path

from decimal import Decimal
from collections import Counter
from datetime import datetime, timezone
import math

from sqlalchemy import and_, case, create_engine, func, inspect, or_, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from .db_models import (
    AuditEventRecord, Base, DocumentAclRecord, DocumentChunkRecord, DocumentRecord,
    DocumentVersionRecord, ModelUsageRecord, QueryRecord, RuntimeModelConfigRecord,
)
from .pricing import cost_cny, pricing_status
from .text_index import lexical_text, lexical_terms


def _database_url(value: str) -> str:
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    if "://" in value:
        return value
    return f"sqlite:///{Path(value).resolve().as_posix()}"


class QueryDatabase:
    """Repository boundary; PostgreSQL schema ownership remains with Alembic."""

    def __init__(self, url_or_path: str, *, pool_size: int = 5, max_overflow: int = 10,
                 pool_timeout: int = 30, connect_timeout: int = 5):
        self.url = _database_url(str(url_or_path))
        url = make_url(self.url)
        engine_options: dict = {"pool_pre_ping": True}
        if url.get_backend_name() == "sqlite":
            database = url.database or ""
            if database and database != ":memory:":
                Path(database).parent.mkdir(parents=True, exist_ok=True)
            engine_options["connect_args"] = {"check_same_thread": False}
            engine_options["poolclass"] = NullPool
        elif url.get_backend_name() == "postgresql":
            engine_options.update({
                "pool_size": pool_size,
                "max_overflow": max_overflow,
                "pool_timeout": pool_timeout,
                "connect_args": {"connect_timeout": connect_timeout},
            })
        self.engine: Engine = create_engine(self.url, **engine_options)
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    @property
    def backend(self) -> str:
        return self.engine.url.get_backend_name()

    def initialize(self) -> None:
        """Create only disposable SQLite schemas; PostgreSQL uses Alembic exclusively."""
        if self.backend == "sqlite":
            inspector = inspect(self.engine)
            if not inspector.has_table("alembic_version"):
                Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()

    def healthcheck(self) -> tuple[bool, str]:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            tables = set(inspect(self.engine).get_table_names())
            required = {
                QueryRecord.__tablename__, ModelUsageRecord.__tablename__,
                DocumentRecord.__tablename__, DocumentVersionRecord.__tablename__,
                DocumentChunkRecord.__tablename__, DocumentAclRecord.__tablename__,
                AuditEventRecord.__tablename__, RuntimeModelConfigRecord.__tablename__,
            }
            if not required.issubset(tables):
                return False, "database_schema_missing"
            return True, "ok"
        except (OSError, SQLAlchemyError):
            return False, "database_unavailable"

    def record(self, session_id: str, question: str, evidence: str, model_route: str,
               owner_subject_id: str = "legacy") -> int:
        self.initialize()
        row = QueryRecord(
            session_id=str(session_id or "default")[:128],
            owner_subject_id=owner_subject_id[:64],
            question=question,
            evidence=evidence,
            model_route=model_route,
        )
        with self._sessions.begin() as session:
            session.add(row)
        return int(row.id)

    def update_query_route(self, query_id: int, model_route: str, evidence: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(QueryRecord, query_id)
            if row is not None:
                row.model_route = model_route
                row.evidence = evidence

    def history(self, session_id: str, limit: int = 20,
                owner_subject_id: str = "legacy") -> list[dict]:
        self.initialize()
        count = max(1, min(int(limit), 100))
        statement = (
            select(QueryRecord)
            .where(QueryRecord.session_id == str(session_id or "default")[:128])
            .where(QueryRecord.owner_subject_id == owner_subject_id[:64])
            .order_by(QueryRecord.id.desc())
            .limit(count)
        )
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [
            {
                "id": row.id,
                "question": row.question,
                "evidence": row.evidence,
                "model_route": row.model_route,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    def record_model_attempts(self, query_id: int, request_id: str, route: dict, attempts) -> None:
        pricing = pricing_status(route["provider"], route["model"])
        input_price = Decimal(str(pricing["input"])) if pricing["known"] else None
        output_price = Decimal(str(pricing["output"])) if pricing["known"] else None
        rows = []
        for attempt in attempts:
            charge = None
            if attempt.usage_reported and pricing["known"]:
                charge = Decimal(str(cost_cny(
                    route["provider"], route["model"],
                    attempt.prompt_tokens or 0, attempt.completion_tokens or 0,
                )))
            rows.append(ModelUsageRecord(
                query_id=query_id,
                document_version_id=None,
                operation="chat",
                request_id=request_id[:128],
                provider=route["provider"],
                model=route["model"][:128],
                route=route["route"],
                attempt=attempt.attempt,
                status=attempt.status,
                prompt_tokens=attempt.prompt_tokens,
                completion_tokens=attempt.completion_tokens,
                total_tokens=attempt.total_tokens,
                usage_reported=attempt.usage_reported,
                input_price_cny=input_price,
                output_price_cny=output_price,
                cost_cny=charge,
                provider_request_id=attempt.provider_request_id or None,
                latency_ms=attempt.latency_ms,
                error_code=attempt.error_code,
            ))
        if rows:
            with self._sessions.begin() as session:
                session.add_all(rows)

    def record_embedding_usages(self, *, usages, request_id: str,
                                query_id: int | None = None,
                                document_version_id: int | None = None) -> None:
        rows = [ModelUsageRecord(
            query_id=query_id,
            document_version_id=document_version_id,
            operation="embedding",
            request_id=request_id[:128],
            provider=usage.provider,
            model=usage.model[:128],
            route="embedding",
            attempt=index,
            status=usage.status,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=0 if usage.prompt_tokens is not None else None,
            total_tokens=usage.total_tokens,
            usage_reported=usage.usage_reported,
            input_price_cny=None,
            output_price_cny=None,
            cost_cny=None,
            provider_request_id=usage.provider_request_id or None,
            latency_ms=usage.latency_ms,
            error_code=usage.error_code,
        ) for index, usage in enumerate(usages, 1)]
        if rows:
            with self._sessions.begin() as session:
                session.add_all(rows)

    def begin_document_import(self, *, source_key: str, title: str, mime_type: str,
                              content_sha256: str, access_scope: str | None = None,
                              classification: str | None = None) -> dict:
        normalized_scope = str(access_scope).strip().lower() if access_scope is not None else None
        if normalized_scope is not None and normalized_scope not in {"public", "restricted"}:
            raise ValueError("文档访问范围无效")
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            document = session.scalar(select(DocumentRecord).where(
                DocumentRecord.source_key == source_key[:512],
            ))
            if document is None:
                document = DocumentRecord(
                    source_key=source_key[:512], title=title[:512], mime_type=mime_type,
                    access_scope=normalized_scope or "public",
                    classification=(classification or "internal")[:32],
                    created_at=now, updated_at=now,
                )
                session.add(document)
                session.flush()
            else:
                document.title = title[:512]
                document.mime_type = mime_type
                if normalized_scope is not None:
                    document.access_scope = normalized_scope
                if classification is not None:
                    document.classification = classification[:32]
                document.updated_at = now
            existing = session.scalar(select(DocumentVersionRecord).where(
                DocumentVersionRecord.document_id == document.id,
                DocumentVersionRecord.content_sha256 == content_sha256,
            ))
            if existing is not None and existing.status in {"indexed", "pending"}:
                return {
                    "document_id": document.id, "version_id": existing.id,
                    "version": existing.version, "duplicate": True, "status": existing.status,
                }
            if existing is not None:
                existing.status = "pending"
                existing.error_code = None
                version = existing
            else:
                latest = session.scalar(select(func.max(DocumentVersionRecord.version)).where(
                    DocumentVersionRecord.document_id == document.id,
                )) or 0
                version = DocumentVersionRecord(
                    document_id=document.id,
                    version=int(latest) + 1,
                    content_sha256=content_sha256,
                    status="pending",
                    chunk_count=0,
                    created_at=now,
                )
                session.add(version)
                session.flush()
            return {
                "document_id": document.id, "version_id": version.id,
                "version": version.version, "duplicate": False, "status": version.status,
            }

    def complete_document_import(self, version_id: int, chunks, vectors) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("文档块和向量数量不一致")
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            version = session.get(DocumentVersionRecord, version_id)
            if version is None:
                raise ValueError("文档版本不存在")
            session.query(DocumentChunkRecord).filter(
                DocumentChunkRecord.document_version_id == version_id,
            ).delete()
            session.add_all([
                DocumentChunkRecord(
                    document_version_id=version_id,
                    ordinal=chunk.ordinal,
                    heading=chunk.heading[:512],
                    page_number=chunk.page_number,
                    content=chunk.content,
                    search_text=lexical_text(f"{chunk.heading} {chunk.content}"),
                    embedding=list(vector),
                    created_at=now,
                )
                for chunk, vector in zip(chunks, vectors)
            ])
            session.query(DocumentVersionRecord).filter(
                DocumentVersionRecord.document_id == version.document_id,
                DocumentVersionRecord.id != version.id,
                DocumentVersionRecord.status == "indexed",
            ).update({"status": "superseded"})
            version.status = "indexed"
            version.chunk_count = len(chunks)
            version.error_code = None
            version.published_at = now

    def fail_document_import(self, version_id: int, error_code: str) -> None:
        with self._sessions.begin() as session:
            version = session.get(DocumentVersionRecord, version_id)
            if version is not None:
                version.status = "failed"
                version.error_code = error_code[:64]

    def list_documents(self) -> list[dict]:
        statement = (
            select(DocumentRecord, DocumentVersionRecord)
            .join(DocumentVersionRecord, DocumentVersionRecord.document_id == DocumentRecord.id)
            .order_by(DocumentRecord.id, DocumentVersionRecord.version.desc())
        )
        with self._sessions() as session:
            rows = session.execute(statement).all()
        return [{
            "document_id": document.id,
            "title": document.title,
            "source_key": document.source_key,
            "mime_type": document.mime_type,
            "access_scope": document.access_scope,
            "classification": document.classification,
            "version": version.version,
            "status": version.status,
            "chunk_count": version.chunk_count,
            "created_at": version.created_at.isoformat(),
        } for document, version in rows]

    def document_usage_ledger(self, version_id: int) -> list[dict]:
        statement = (
            select(ModelUsageRecord)
            .where(ModelUsageRecord.document_version_id == version_id)
            .order_by(ModelUsageRecord.id)
        )
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [self._usage_public(row) for row in rows]

    def has_indexed_chunks(self) -> bool:
        statement = (
            select(func.count(DocumentChunkRecord.id))
            .join(DocumentVersionRecord)
            .where(DocumentVersionRecord.status == "indexed")
        )
        with self._sessions() as session:
            return bool(session.scalar(statement))

    def accessible_document_outline(self, *, subject_id: str = "legacy", roles=(), groups=(),
                                    max_documents: int = 20,
                                    max_sections: int = 8) -> list[dict]:
        """Return an ACL-filtered outline without exposing document contents."""
        normalized_roles = {str(item).strip().lower() for item in roles if str(item).strip()}
        normalized_groups = {str(item).strip().lower() for item in groups if str(item).strip()}
        acl_matches = [and_(
            DocumentAclRecord.principal_type == "user",
            DocumentAclRecord.principal_id == subject_id.lower(),
        )]
        if normalized_roles:
            acl_matches.append(and_(
                DocumentAclRecord.principal_type == "role",
                DocumentAclRecord.principal_id.in_(normalized_roles),
            ))
        if normalized_groups:
            acl_matches.append(and_(
                DocumentAclRecord.principal_type == "group",
                DocumentAclRecord.principal_id.in_(normalized_groups),
            ))
        allowed = or_(
            DocumentRecord.access_scope == "public",
            select(DocumentAclRecord.id).where(
                DocumentAclRecord.document_id == DocumentRecord.id,
                or_(*acl_matches),
            ).exists(),
        )
        document_statement = (
            select(DocumentRecord, DocumentVersionRecord)
            .join(DocumentVersionRecord, DocumentVersionRecord.document_id == DocumentRecord.id)
            .where(DocumentVersionRecord.status == "indexed", allowed)
            .order_by(DocumentRecord.title, DocumentRecord.id)
            .limit(max(1, min(int(max_documents), 100)))
        )
        with self._sessions() as session:
            documents = session.execute(document_statement).all()
            version_ids = [version.id for _, version in documents]
            chunks = session.execute(
                select(DocumentChunkRecord)
                .where(DocumentChunkRecord.document_version_id.in_(version_ids))
                .order_by(DocumentChunkRecord.document_version_id, DocumentChunkRecord.ordinal)
            ).scalars().all() if version_ids else []
        chunks_by_version: dict[int, list[DocumentChunkRecord]] = {}
        for chunk in chunks:
            chunks_by_version.setdefault(chunk.document_version_id, []).append(chunk)
        result = []
        section_limit = max(1, min(int(max_sections), 50))
        for document, version in documents:
            headings, first_chunks = [], {}
            for chunk in chunks_by_version.get(version.id, []):
                heading = (chunk.heading or "正文").strip()
                if heading not in first_chunks:
                    headings.append(heading)
                    first_chunks[heading] = chunk
                if len(headings) >= section_limit:
                    break
            first = first_chunks.get(headings[0]) if headings else None
            result.append({
                "title": document.title,
                "version": version.version,
                "sections": headings,
                "chunk": (first.ordinal + 1) if first else None,
                "section": headings[0] if headings else "",
                "page": first.page_number if first else None,
            })
        return result

    def hybrid_search(self, query: str, embedding: list[float], limit: int = 5,
                      *, subject_id: str = "legacy", roles=(), groups=()) -> list[dict]:
        if self.backend == "postgresql":
            return self._postgres_hybrid_search(query, embedding, limit, subject_id, roles, groups)
        return self._portable_hybrid_search(query, embedding, limit, subject_id, roles, groups)

    def lexical_search(self, query: str, limit: int = 5, *, subject_id: str = "legacy",
                       roles=(), groups=()) -> list[dict]:
        if self.backend == "postgresql":
            statement = text("""
                SELECT c.id, d.title, v.version, c.ordinal, c.heading, c.page_number,
                       c.content, ts_rank_cd(
                           c.search_vector, websearch_to_tsquery('simple', :query)
                       ) AS score
                FROM document_chunks c
                JOIN document_versions v ON v.id = c.document_version_id
                JOIN documents d ON d.id = v.document_id
                WHERE v.status = 'indexed'
                  AND (d.access_scope = 'public' OR EXISTS (
                    SELECT 1 FROM document_acl a
                    WHERE a.document_id = d.id AND (
                      (a.principal_type = 'user' AND a.principal_id = :subject_id)
                      OR (a.principal_type = 'role' AND a.principal_id = ANY(string_to_array(:acl_roles, ',')))
                      OR (a.principal_type = 'group' AND a.principal_id = ANY(string_to_array(:acl_groups, ',')))
                    )
                  ))
                  AND c.search_vector @@ websearch_to_tsquery('simple', :query)
                ORDER BY score DESC LIMIT :result_limit
            """)
            with self.engine.connect() as connection:
                return [dict(row) for row in connection.execute(statement, {
                    "query": self._postgres_websearch_query(query), "result_limit": limit,
                    "subject_id": subject_id, "acl_roles": ",".join(roles),
                    "acl_groups": ",".join(groups),
                }).mappings().all()]
        results = self._portable_hybrid_search(
            query, [0.0] * 1024, max(limit * 4, 20), subject_id, roles, groups,
        )
        lexical = [item for item in results if item.get("lexical_rank") is not None]
        return sorted(lexical, key=lambda item: item["lexical_rank"])[:limit]

    def _postgres_hybrid_search(self, query: str, embedding: list[float], limit: int,
                                subject_id: str, roles, groups) -> list[dict]:
        statement = text("""
            WITH eligible AS (
                SELECT c.*, d.title, v.version
                FROM document_chunks c
                JOIN document_versions v ON v.id = c.document_version_id
                JOIN documents d ON d.id = v.document_id
                WHERE v.status = 'indexed'
                  AND (d.access_scope = 'public' OR EXISTS (
                    SELECT 1 FROM document_acl a
                    WHERE a.document_id = d.id AND (
                      (a.principal_type = 'user' AND a.principal_id = :subject_id)
                      OR (a.principal_type = 'role' AND a.principal_id = ANY(string_to_array(:acl_roles, ',')))
                      OR (a.principal_type = 'group' AND a.principal_id = ANY(string_to_array(:acl_groups, ',')))
                    )
                  ))
            ), lexical AS (
                SELECT id, row_number() OVER (ORDER BY lexical_score DESC) AS lexical_rank
                FROM (
                    SELECT id, ts_rank_cd(
                        search_vector, websearch_to_tsquery('simple', :query)
                    ) lexical_score
                    FROM eligible
                    WHERE search_vector @@ websearch_to_tsquery('simple', :query)
                    ORDER BY lexical_score DESC LIMIT :candidate_limit
                ) ranked
            ), semantic AS (
                SELECT id, similarity,
                       row_number() OVER (ORDER BY similarity DESC) AS semantic_rank
                FROM (
                    SELECT id, 1 - (embedding <=> CAST(:embedding AS vector)) similarity
                    FROM eligible
                    ORDER BY embedding <=> CAST(:embedding AS vector)
                    LIMIT :candidate_limit
                ) ranked
            ), candidates AS (
                SELECT id FROM lexical UNION SELECT id FROM semantic
            )
            SELECT e.id, e.title, e.version, e.ordinal, e.heading, e.page_number, e.content,
                   l.lexical_rank, s.semantic_rank, s.similarity,
                   COALESCE(1.0 / (60 + l.lexical_rank), 0) +
                   COALESCE(1.0 / (60 + s.semantic_rank), 0) AS score
            FROM candidates x
            JOIN eligible e ON e.id = x.id
            LEFT JOIN lexical l ON l.id = x.id
            LEFT JOIN semantic s ON s.id = x.id
            WHERE l.lexical_rank IS NOT NULL OR s.similarity >= :semantic_threshold
            ORDER BY score DESC LIMIT :result_limit
        """)
        vector_value = "[" + ",".join(f"{value:.10f}" for value in embedding) + "]"
        with self.engine.connect() as connection:
            rows = connection.execute(statement, {
                "query": self._postgres_websearch_query(query),
                "embedding": vector_value,
                "candidate_limit": max(20, limit * 5),
                "semantic_threshold": 0.35,
                "result_limit": limit,
                "subject_id": subject_id, "acl_roles": ",".join(roles),
                "acl_groups": ",".join(groups),
            }).mappings().all()
        return [dict(row) for row in rows]

    def _portable_hybrid_search(self, query: str, embedding: list[float], limit: int,
                                subject_id: str = "legacy", roles=(), groups=()) -> list[dict]:
        statement = (
            select(DocumentChunkRecord, DocumentRecord, DocumentVersionRecord.version)
            .join(DocumentVersionRecord, DocumentVersionRecord.id == DocumentChunkRecord.document_version_id)
            .join(DocumentRecord, DocumentRecord.id == DocumentVersionRecord.document_id)
            .where(DocumentVersionRecord.status == "indexed")
        )
        with self._sessions() as session:
            rows = session.execute(statement).all()
            document_ids = {document.id for _, document, _ in rows}
            acl_rows = session.execute(
                select(
                    DocumentAclRecord.document_id,
                    DocumentAclRecord.principal_type,
                    DocumentAclRecord.principal_id,
                ).where(DocumentAclRecord.document_id.in_(document_ids))
            ).all() if document_ids else []
        acl_by_document: dict[int, set[tuple[str, str]]] = {}
        for document_id, principal_type, principal_id in acl_rows:
            acl_by_document.setdefault(document_id, set()).add((principal_type, principal_id))
        roles = {str(item).lower() for item in roles}
        groups = {str(item).lower() for item in groups}
        rows = [row for row in rows if self._document_allowed(
            row[1], subject_id, roles, groups, acl_by_document.get(row[1].id, set()),
        )]
        if not rows:
            return []
        query_terms = lexical_terms(query)
        term_counts = [Counter((chunk.search_text or "").split()) for chunk, _, _ in rows]
        average_length = sum(sum(counts.values()) for counts in term_counts) / len(term_counts)
        document_frequency = Counter(
            term for counts in term_counts for term in set(counts) if term in query_terms
        )
        lexical_scores: dict[int, float] = {}
        semantic_scores: dict[int, float] = {}
        for (chunk, _, _), counts in zip(rows, term_counts):
            length = sum(counts.values()) or 1
            score = 0.0
            for term in query_terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                inverse = math.log(1 + (len(rows) - document_frequency[term] + 0.5) /
                                   (document_frequency[term] + 0.5))
                score += inverse * frequency / (
                    frequency + 1.2 * (0.25 + 0.75 * length / (average_length or 1))
                )
            lexical_scores[chunk.id] = score
            semantic_scores[chunk.id] = self._cosine(embedding, chunk.embedding)
        lexical_rank = {
            chunk_id: rank for rank, (chunk_id, score) in enumerate(
                sorted(lexical_scores.items(), key=lambda item: item[1], reverse=True), 1,
            ) if score > 0
        }
        semantic_rank = {
            chunk_id: rank for rank, (chunk_id, _score) in enumerate(
                sorted(semantic_scores.items(), key=lambda item: item[1], reverse=True), 1,
            )
        }
        results = []
        for chunk, document, version in rows:
            similarity = semantic_scores[chunk.id]
            if chunk.id not in lexical_rank and similarity < 0.35:
                continue
            score = (
                (1 / (60 + lexical_rank[chunk.id])) if chunk.id in lexical_rank else 0
            ) + (1 / (60 + semantic_rank[chunk.id]))
            results.append({
                "id": chunk.id, "title": document.title, "version": version,
                "ordinal": chunk.ordinal, "heading": chunk.heading,
                "page_number": chunk.page_number, "content": chunk.content,
                "lexical_rank": lexical_rank.get(chunk.id),
                "semantic_rank": semantic_rank[chunk.id],
                "similarity": similarity, "score": score,
            })
        return sorted(results, key=lambda item: item["score"], reverse=True)[:limit]

    @staticmethod
    def _document_allowed(document, subject_id: str, roles, groups, entries) -> bool:
        if document.access_scope == "public":
            return True
        return (
            ("user", subject_id.lower()) in entries
            or any(("role", role) in entries for role in roles)
            or any(("group", group) in entries for group in groups)
        )

    def set_document_acl(self, document_id: int, entries, *, actor_subject_id: str,
                         request_id: str, access_scope: str = "restricted") -> None:
        allowed_types = {"user", "group", "role"}
        access_scope = str(access_scope).strip().lower()
        if access_scope not in {"public", "restricted"}:
            raise ValueError("文档访问范围无效")
        normalized = {(str(kind).lower(), str(value).strip().lower()) for kind, value in entries}
        if any(
            kind not in allowed_types or not value or len(value) > 256
            for kind, value in normalized
        ):
            raise ValueError("文档 ACL 主体格式无效")
        if access_scope == "public":
            normalized = set()
        with self._sessions.begin() as session:
            document = session.get(DocumentRecord, document_id)
            if document is None:
                raise ValueError("文档不存在")
            document.access_scope = access_scope
            document.updated_at = datetime.now(timezone.utc)
            session.query(DocumentAclRecord).filter(
                DocumentAclRecord.document_id == document_id,
            ).delete()
            session.add_all(DocumentAclRecord(
                document_id=document_id, principal_type=kind, principal_id=value,
                created_by_subject_id=actor_subject_id[:64],
            ) for kind, value in sorted(normalized))
            session.add(AuditEventRecord(
                actor_subject_id=actor_subject_id[:64], action="document_acl_replace",
                target_type="document", target_ref=str(document_id), result="success",
                request_id=request_id[:128],
            ))

    def document_acl(self, document_id: int) -> list[dict]:
        statement = select(DocumentAclRecord).where(
            DocumentAclRecord.document_id == document_id,
        ).order_by(DocumentAclRecord.id)
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [{"principal_type": row.principal_type, "principal_id": row.principal_id}
                for row in rows]

    def document_access(self, document_id: int) -> dict:
        with self._sessions() as session:
            document = session.get(DocumentRecord, document_id)
        if document is None:
            raise ValueError("文档不存在")
        return {
            "document_id": document.id,
            "access_scope": document.access_scope,
            "classification": document.classification,
            "entries": self.document_acl(document_id),
        }

    def record_audit_event(self, *, actor_subject_id: str, action: str,
                           target_type: str, target_ref: str, result: str,
                           request_id: str) -> None:
        with self._sessions.begin() as session:
            session.add(AuditEventRecord(
                actor_subject_id=actor_subject_id[:64],
                action=action[:64],
                target_type=target_type[:32],
                target_ref=target_ref[:512],
                result=result[:16],
                request_id=request_id[:128],
            ))

    def runtime_model_config(self) -> dict | None:
        try:
            with self._sessions() as session:
                row = session.get(RuntimeModelConfigRecord, 1)
        except (OSError, SQLAlchemyError):
            return None
        if row is None:
            return None
        return {
            "mode": row.mode,
            "provider": row.provider,
            "model": row.model,
            "response_strategy": getattr(row, "response_strategy", "knowledge_first") or "knowledge_first",
            "updated_by_subject_id": row.updated_by_subject_id,
            "updated_at": row.updated_at.isoformat(),
        }

    def set_runtime_model_config(self, *, mode: str, provider: str, model: str,
                                 response_strategy: str = "knowledge_first",
                                 actor_subject_id: str, request_id: str) -> dict:
        now = datetime.now(timezone.utc)
        with self._sessions.begin() as session:
            row = session.get(RuntimeModelConfigRecord, 1)
            if row is None:
                row = RuntimeModelConfigRecord(id=1)
                session.add(row)
            row.mode = mode[:16]
            row.provider = provider[:32]
            row.model = model[:128]
            row.response_strategy = response_strategy[:32]
            row.updated_by_subject_id = actor_subject_id[:64]
            row.updated_at = now
            session.add(AuditEventRecord(
                actor_subject_id=actor_subject_id[:64], action="model_config_update",
                target_type="model_config", target_ref=f"{mode}:{provider}:{model}"[:512],
                result="success", request_id=request_id[:128],
            ))
        return self.runtime_model_config() or {}

    def audit_events(self, limit: int = 100) -> list[dict]:
        count = max(1, min(int(limit), 200))
        statement = select(AuditEventRecord).order_by(
            AuditEventRecord.id.desc(),
        ).limit(count)
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [{
            "id": row.id,
            "actor_subject_id": row.actor_subject_id,
            "action": row.action,
            "target_type": row.target_type,
            "target_ref": row.target_ref,
            "result": row.result,
            "request_id": row.request_id,
            "created_at": row.created_at.isoformat(),
        } for row in rows]

    @staticmethod
    def _cosine(left, right) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        numerator = sum(float(a) * float(b) for a, b in zip(left, right))
        left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
        right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return max(-1.0, min(1.0, numerator / (left_norm * right_norm)))

    @staticmethod
    def _postgres_websearch_query(query: str) -> str:
        return " OR ".join(f'"{term}"' for term in lexical_terms(query))

    def usage_ledger(self, session_id: str, limit: int = 50,
                     owner_subject_id: str = "legacy") -> list[dict]:
        count = max(1, min(int(limit), 100))
        statement = (
            select(ModelUsageRecord)
            .join(QueryRecord, QueryRecord.id == ModelUsageRecord.query_id)
            .where(QueryRecord.session_id == str(session_id or "default")[:128])
            .where(QueryRecord.owner_subject_id == owner_subject_id[:64])
            .order_by(ModelUsageRecord.id.desc())
            .limit(count)
        )
        with self._sessions() as session:
            rows = session.scalars(statement).all()
        return [self._usage_public(row) for row in rows]

    def usage_summary(self, session_id: str, owner_subject_id: str = "legacy") -> dict:
        statement = (
            select(
                func.count(ModelUsageRecord.id),
                func.sum(case((ModelUsageRecord.status == "succeeded", 1), else_=0)),
                func.coalesce(func.sum(ModelUsageRecord.prompt_tokens), 0),
                func.coalesce(func.sum(ModelUsageRecord.completion_tokens), 0),
                func.coalesce(func.sum(ModelUsageRecord.total_tokens), 0),
                func.coalesce(func.sum(ModelUsageRecord.cost_cny), 0),
                func.sum(case((
                    (ModelUsageRecord.status == "succeeded")
                    & ModelUsageRecord.usage_reported
                    & (ModelUsageRecord.cost_cny.is_(None)), 1
                ), else_=0)),
                func.sum(case((
                    (ModelUsageRecord.status == "succeeded")
                    & (ModelUsageRecord.usage_reported.is_(False)), 1
                ), else_=0)),
            )
            .join(QueryRecord, QueryRecord.id == ModelUsageRecord.query_id)
            .where(QueryRecord.session_id == str(session_id or "default")[:128])
            .where(QueryRecord.owner_subject_id == owner_subject_id[:64])
        )
        with self._sessions() as session:
            row = session.execute(statement).one()
        return {
            "attempts": int(row[0] or 0),
            "successful_calls": int(row[1] or 0),
            "prompt_tokens": int(row[2] or 0),
            "completion_tokens": int(row[3] or 0),
            "total_tokens": int(row[4] or 0),
            "cost_cny": float(row[5] or 0),
            "unpriced_calls": int(row[6] or 0),
            "unmetered_calls": int(row[7] or 0),
            "currency": "CNY",
        }

    @staticmethod
    def _usage_public(row: ModelUsageRecord) -> dict:
        return {
            "id": row.id,
            "query_id": row.query_id,
            "document_version_id": row.document_version_id,
            "operation": row.operation,
            "request_id": row.request_id,
            "provider": row.provider,
            "model": row.model,
            "route": row.route,
            "attempt": row.attempt,
            "status": row.status,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "total_tokens": row.total_tokens,
            "usage_reported": row.usage_reported,
            "cost_cny": float(row.cost_cny) if row.cost_cny is not None else None,
            "input_price_cny_per_million": (
                float(row.input_price_cny) if row.input_price_cny is not None else None
            ),
            "output_price_cny_per_million": (
                float(row.output_price_cny) if row.output_price_cny is not None else None
            ),
            "provider_request_id": row.provider_request_id,
            "latency_ms": row.latency_ms,
            "error_code": row.error_code,
            "created_at": row.created_at.isoformat(),
        }
