"""Administrative document ingestion command; intentionally not exposed by the query API."""
from __future__ import annotations

import argparse
import json
import uuid

from backend import AppSettings, GovernanceError, QueryDatabase
from backend.embeddings import build_embedding_client

from .service import DocumentIngestionService


def main() -> int:
    parser = argparse.ArgumentParser(prog="docmind-ingest")
    subcommands = parser.add_subparsers(dest="command", required=True)
    importer = subcommands.add_parser("import", help="Import or version one document")
    importer.add_argument("path")
    importer.add_argument("--title", default="")
    importer.add_argument("--source-key", default="")
    importer.add_argument("--access-scope", choices=("public", "restricted"), default="restricted")
    importer.add_argument("--classification", default="internal")
    subcommands.add_parser("list", help="List imported document versions")
    args = parser.parse_args()

    settings = AppSettings.from_environment()
    database = QueryDatabase(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout,
        connect_timeout=settings.database_connect_timeout,
    )
    try:
        if args.command == "list":
            result = {"ok": True, "items": database.list_documents()}
        else:
            service = DocumentIngestionService(
                database,
                build_embedding_client(settings),
                max_bytes=settings.document_max_bytes,
                chunk_max_chars=settings.chunk_max_chars,
                chunk_overlap_chars=settings.chunk_overlap_chars,
                max_characters=settings.document_max_characters,
                max_pages=settings.document_max_pages,
                require_review=settings.governance_mode == "review",
            )
            try:
                result = {"ok": True, **service.import_file(
                    args.path, title=args.title, source_key=args.source_key,
                    access_scope=args.access_scope, classification=args.classification,
                    submitted_by_subject_id="cli", request_id=f"cli-{uuid.uuid4().hex}",
                )}
            except GovernanceError as exc:
                print(json.dumps({"ok": False, "error": str(exc), "code": exc.code},
                                 ensure_ascii=False, indent=2))
                return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
