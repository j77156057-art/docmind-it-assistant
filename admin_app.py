"""Independent administrative surface for document lifecycle and access control."""
from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
from typing import Any, Literal
import uuid

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from backend import (
    AppSettings, AuthenticationError, EmbeddingClient, OIDCAuthenticator, Principal,
    DocumentSourceStore, ModelRouter, ModelRuntime, ModelRuntimeError, QueryDatabase,
    build_embedding_client, configure_logging, log_event, request_id_context,
)
from ingestion import DocumentIngestionService
from backend.artifacts import ARTIFACT_MEDIA_TYPES, ArtifactError, ArtifactService


LOGGER = logging.getLogger("docmind.it.admin")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf", ".docx"}


class AclEntryReq(BaseModel):
    principal_type: Literal["user", "group", "role"]
    principal_id: str = Field(min_length=1, max_length=256)


class DocumentAclReq(BaseModel):
    access_scope: Literal["public", "restricted"] = "restricted"
    entries: list[AclEntryReq] = Field(default_factory=list)


class ArtifactCreateReq(BaseModel):
    format: Literal["docx", "pdf", "pptx", "xlsx"]
    filename: str = Field(min_length=1, max_length=128)
    title: str = Field(default="", max_length=512)
    subtitle: str = Field(default="", max_length=1024)
    paragraphs: list[str] = Field(default_factory=list, max_length=100)
    bullets: list[str] = Field(default_factory=list, max_length=100)
    sections: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    slides: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    sheets: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    table: dict[str, Any] | None = None
    allow_formulas: bool = False


class ModelConfigReq(BaseModel):
    mode: Literal["knowledge", "local", "cloud"]
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=128)
    response_strategy: Literal["knowledge_first", "generative_first", "hybrid"] = "knowledge_first"


def create_admin_app(settings: AppSettings | None = None,
                     embedding_client: EmbeddingClient | None = None,
                     model_runtime: ModelRuntime | None = None) -> FastAPI:
    config = settings or AppSettings.from_environment()
    configure_logging(config)
    database = QueryDatabase(
        config.database_url,
        pool_size=config.database_pool_size,
        max_overflow=config.database_max_overflow,
        pool_timeout=config.database_pool_timeout,
        connect_timeout=config.database_connect_timeout,
    )
    embeddings = embedding_client or build_embedding_client(config)
    ingestion = DocumentIngestionService(
        database,
        embeddings,
        max_bytes=config.document_max_bytes,
        chunk_max_chars=config.chunk_max_chars,
        chunk_overlap_chars=config.chunk_overlap_chars,
        max_characters=config.document_max_characters,
        max_pages=config.document_max_pages,
    )
    authenticator = OIDCAuthenticator(
        mode=config.auth_mode,
        issuer=config.oidc_issuer,
        audience=config.oidc_audience,
        jwks_url=config.oidc_jwks_url,
        subject_salt=config.auth_subject_salt.get_secret_value(),
        role_claim=config.oidc_role_claim,
        group_claim=config.oidc_group_claim,
        leeway_seconds=config.oidc_leeway_seconds,
    )
    artifacts = ArtifactService(config.artifact_output_path)
    models = ModelRouter.from_settings(config, runtime_loader=database.runtime_model_config)
    sources = DocumentSourceStore(config.project_root / "data" / "sources")
    runtime = model_runtime or ModelRuntime(timeout_seconds=max(60.0, config.model_timeout_seconds))

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        database.initialize()
        artifacts.initialize()
        sources.initialize()
        log_event(LOGGER, logging.INFO, "admin_application_started", environment=config.environment)
        yield
        database.dispose()
        log_event(LOGGER, logging.INFO, "admin_application_stopped", environment=config.environment)

    application = FastAPI(title=f"{config.app_name} Admin", lifespan=lifespan)
    application.state.settings = config
    application.state.database = database
    application.state.embeddings = embeddings
    application.state.ingestion = ingestion
    application.state.authenticator = authenticator
    application.state.artifacts = artifacts
    application.state.models = models
    application.state.sources = sources
    application.state.model_runtime = runtime

    @application.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        return JSONResponse(
            {"ok": False, "error": str(exc.detail)},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    def authenticated(request: Request) -> Principal:
        try:
            return authenticator.authenticate(request.headers)
        except AuthenticationError as exc:
            log_event(LOGGER, logging.WARNING, "admin_authentication_failed", reason=exc.code)
            raise HTTPException(
                status_code=401,
                detail="未认证或登录已失效",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None

    def require_role(role: str):
        def authorize(principal: Principal = Depends(authenticated)) -> Principal:
            if not principal.allows(role):
                log_event(LOGGER, logging.WARNING, "admin_authorization_denied", reason=role)
                raise HTTPException(status_code=403, detail="权限不足")
            return principal
        return authorize

    auditor = require_role("auditor")
    admin = require_role("admin")

    @application.middleware("http")
    async def request_boundary(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "").strip()
        request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else uuid.uuid4().hex
        token = request_id_context.set(request_id)
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001 - privacy-safe boundary response
                log_event(
                    LOGGER, logging.ERROR, "admin_request_failed",
                    method=request.method, path=request.url.path,
                    status_code=500, error_type=type(exc).__name__,
                )
                response = JSONResponse(
                    {"ok": False, "error": "管理服务内部错误", "request_id": request_id},
                    status_code=500,
                )
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
            )
            log_event(
                LOGGER, logging.INFO, "admin_request_completed",
                method=request.method, path=request.url.path,
                status_code=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            return response
        finally:
            request_id_context.reset(token)

    @application.get("/")
    async def admin_index():
        return FileResponse(config.admin_index_path)

    @application.get("/assets/admin.css")
    async def admin_styles():
        return FileResponse(config.admin_index_path.with_name("admin.css"), media_type="text/css")

    @application.get("/assets/admin.js")
    async def admin_script():
        return FileResponse(
            config.admin_index_path.with_name("admin.js"), media_type="text/javascript",
        )

    @application.get("/health/live")
    async def health_live():
        return {"ok": True, "status": "live"}

    @application.get("/health/ready")
    async def health_ready():
        database_ok, database_reason = database.healthcheck()
        embedding_ok, embedding_reason = embeddings.healthcheck()
        auth_ok, auth_reason = authenticator.healthcheck()
        artifacts_ok, artifacts_reason = artifacts.healthcheck()
        assets = [
            config.admin_index_path,
            config.admin_index_path.with_name("admin.css"),
            config.admin_index_path.with_name("admin.js"),
        ]
        checks = {
            "database": {"ok": database_ok, "reason": database_reason},
            "embedding": {"ok": embedding_ok, "reason": embedding_reason},
            "authentication": {"ok": auth_ok, "reason": auth_reason},
            "artifact_storage": {"ok": artifacts_ok, "reason": artifacts_reason},
            "admin_web": {
                "ok": all(path.is_file() for path in assets),
                "reason": "ok" if all(path.is_file() for path in assets) else "admin_assets_missing",
            },
        }
        ready = all(item["ok"] for item in checks.values())
        return JSONResponse(
            {"ok": ready, "status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    @application.get("/api/me")
    async def me(principal: Principal = Depends(auditor)):
        return {
            "ok": True,
            "subject_id": principal.subject_id,
            "display_name": principal.display_name,
            "roles": sorted(principal.roles),
            "groups": sorted(principal.groups),
        }

    @application.get("/api/admin/documents")
    async def documents(_principal: Principal = Depends(auditor)):
        items = []
        for item in database.list_documents():
            if item["source_key"] == "builtin/knowledge.md" and item["version"] == 1:
                source = _document_source(item)
                description = (
                    {
                        "source_available": True,
                        "source_filename": source.name,
                        "source_bytes": source.stat().st_size,
                    }
                    if source else sources.describe(item["document_id"], item["version"])
                )
            else:
                description = sources.describe(item["document_id"], item["version"])
            if description["source_available"]:
                base = (
                    f"/api/admin/documents/{item['document_id']}/versions/"
                    f"{item['version']}/source"
                )
                description.update({"source_url": base, "download_url": f"{base}?download=true"})
            items.append({**item, **description})
        return {"ok": True, "items": items}

    def _document_source(item: dict) -> Path | None:
        if item["source_key"] == "builtin/knowledge.md" and item["version"] == 1:
            return config.knowledge_path if config.knowledge_path.is_file() else None
        return sources.resolve(item["document_id"], item["version"])

    @application.get("/api/admin/documents/{document_id}/versions/{version}/source")
    async def document_source(document_id: int, version: int,
                              download: bool = Query(False),
                              principal: Principal = Depends(auditor)):
        item = next((row for row in database.list_documents()
                     if row["document_id"] == document_id and row["version"] == version), None)
        source = _document_source(item) if item else None
        if item is None or source is None:
            raise HTTPException(status_code=404, detail="该版本没有可用的原文件")
        filename = source.name
        stored_prefix = f"v{version}-"
        if filename.startswith(stored_prefix):
            filename = filename[len(stored_prefix):]
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="document_source_download" if download else "document_source_view",
            target_type="document",
            target_ref=f"{document_id}:v{version}",
            result="success",
            request_id=request_id_context.get(),
        )
        if download:
            return FileResponse(source, media_type=item["mime_type"], filename=filename)
        return FileResponse(
            source,
            media_type=item["mime_type"],
            headers={"Content-Disposition": f"inline; filename*=UTF-8''{_quoted_filename(filename)}"},
        )

    @application.get("/api/admin/documents/{document_id}/acl")
    async def document_acl(document_id: int, _principal: Principal = Depends(auditor)):
        try:
            return {"ok": True, **database.document_access(document_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @application.put("/api/admin/documents/{document_id}/acl")
    async def replace_document_acl(document_id: int, payload: DocumentAclReq,
                                   principal: Principal = Depends(admin)):
        try:
            database.set_document_acl(
                document_id,
                ((entry.principal_type, entry.principal_id) for entry in payload.entries),
                actor_subject_id=principal.subject_id,
                request_id=request_id_context.get(),
                access_scope=payload.access_scope,
            )
            return {"ok": True, **database.document_access(document_id)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @application.get("/api/admin/audit-events")
    async def audit_events(limit: int = 100, _principal: Principal = Depends(auditor)):
        return {"ok": True, "items": database.audit_events(limit)}

    @application.get("/api/admin/model-config")
    async def model_config(_principal: Principal = Depends(auditor)):
        active = models.status()
        runtime_status = await run_in_threadpool(
            runtime.status, active, models.base_url({**active, "route": active["mode"]}),
        )
        local_models = {}
        for provider in ("ollama", "llamacpp"):
            provider_route = models._selection("local", provider, "")
            local_models[provider] = await run_in_threadpool(
                runtime.available_models,
                provider,
                models.base_url({**provider_route, "route": "local"}),
            )
        if active["provider"] == "llamacpp" and not runtime_status.get("ready"):
            active_key = _model_match_key(active["model"])
            alternatives = [
                name for name in local_models.get("ollama", {}).get("models", [])
                if _model_match_key(name) == active_key
            ]
            if alternatives:
                runtime_status.update({
                    "state": "wrong_provider",
                    "message": (
                        f"检测到 Ollama 中有同名模型 {alternatives[0]}；当前选择的是 llama.cpp，"
                        "请将供应商切换为“本地 Ollama”"
                    ),
                    "alternative_provider": "ollama",
                    "alternative_models": alternatives,
                })
        return {
            "ok": True,
            "active": active,
            "response_strategy": models.response_strategy(),
            "runtime": runtime_status,
            "local_models": local_models,
            "providers": models.catalog(),
            "override": database.runtime_model_config(),
        }

    @application.put("/api/admin/model-config")
    async def update_model_config(payload: ModelConfigReq,
                                  principal: Principal = Depends(admin)):
        target_ref = f"{payload.mode}:{payload.provider}:{payload.model}:{payload.response_strategy}"
        try:
            selected = models.validate_selection(payload.mode, payload.provider, payload.model)
        except ValueError as exc:
            database.record_audit_event(
                actor_subject_id=principal.subject_id,
                action="model_config_update",
                target_type="model_config",
                target_ref=target_ref,
                result="failed",
                request_id=request_id_context.get(),
            )
            raise HTTPException(status_code=400, detail=str(exc)) from None
        try:
            runtime_status = await run_in_threadpool(
                runtime.activate,
                selected,
                models.base_url({**selected, "route": selected["mode"]}),
            )
        except ModelRuntimeError as exc:
            database.record_audit_event(
                actor_subject_id=principal.subject_id,
                action="model_config_update",
                target_type="model_config",
                target_ref=target_ref,
                result="failed",
                request_id=request_id_context.get(),
            )
            raise HTTPException(status_code=400, detail=str(exc)) from None
        saved = database.set_runtime_model_config(
            mode=selected["mode"],
            provider=selected["provider"],
            model=selected["model"],
            response_strategy=payload.response_strategy,
            actor_subject_id=principal.subject_id,
            request_id=request_id_context.get(),
        )
        return {
            "ok": True, "active": models.status(), "runtime": runtime_status,
            "override": saved,
        }

    @application.get("/api/admin/artifacts")
    async def list_artifacts(_principal: Principal = Depends(auditor)):
        return {"ok": True, "items": await run_in_threadpool(artifacts.list)}

    @application.post("/api/admin/artifacts")
    async def create_artifact(payload: ArtifactCreateReq,
                              principal: Principal = Depends(admin)):
        request_id = request_id_context.get()
        target_ref = payload.filename
        try:
            result = await run_in_threadpool(artifacts.create, payload.model_dump())
            target_ref = result["filename"]
            database.record_audit_event(
                actor_subject_id=principal.subject_id,
                action="artifact_create",
                target_type="artifact",
                target_ref=target_ref,
                result="success",
                request_id=request_id,
            )
            return {"ok": True, **result}
        except ArtifactError as exc:
            database.record_audit_event(
                actor_subject_id=principal.subject_id,
                action="artifact_create",
                target_type="artifact",
                target_ref=target_ref,
                result="failed",
                request_id=request_id,
            )
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @application.get("/api/admin/artifacts/{filename}")
    async def download_artifact(filename: str, _principal: Principal = Depends(auditor)):
        try:
            target = artifacts.resolve(filename)
        except ArtifactError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return FileResponse(
            target,
            media_type=ARTIFACT_MEDIA_TYPES[target.suffix.lower().lstrip(".")],
            filename=target.name,
        )

    @application.post("/api/admin/documents/import")
    async def import_document(
        file: UploadFile = File(...),
        title: str = Form(""),
        source_key: str = Form(""),
        access_scope: str = Form("restricted"),
        classification: str = Form("internal"),
        principal: Principal = Depends(admin),
    ):
        filename = Path(file.filename or "document").name
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise HTTPException(status_code=400, detail="仅支持 Markdown、TXT、PDF 和 DOCX")
        if access_scope not in {"public", "restricted"}:
            raise HTTPException(status_code=400, detail="文档访问范围无效")
        source = source_key.strip() or f"upload/{filename.lower()}"
        request_id = request_id_context.get()
        try:
            with TemporaryDirectory(prefix="docmind-admin-") as root:
                target = Path(root) / f"document{suffix}"
                size = 0
                with target.open("wb") as stream:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > config.document_max_bytes:
                            raise ValueError("文档超过允许的大小")
                        stream.write(chunk)
                result = await run_in_threadpool(
                    ingestion.import_file,
                    target,
                    title=title.strip() or Path(filename).stem,
                    source_key=source,
                    access_scope=access_scope,
                    classification=classification,
                )
                await run_in_threadpool(
                    sources.store,
                    target,
                    document_id=result["document_id"],
                    version=result["version"],
                    filename=filename,
                )
            database.record_audit_event(
                actor_subject_id=principal.subject_id,
                action="document_import",
                target_type="document",
                target_ref=source,
                result="success",
                request_id=request_id,
            )
            return {"ok": True, **result}
        except ValueError as exc:
            database.record_audit_event(
                actor_subject_id=principal.subject_id,
                action="document_import",
                target_type="document",
                target_ref=source,
                result="failed",
                request_id=request_id,
            )
            raise HTTPException(status_code=400, detail=str(exc)) from None
        finally:
            await file.close()

    return application


def _quoted_filename(filename: str) -> str:
    from urllib.parse import quote
    return quote(filename, safe="")


def _model_match_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


app = create_admin_app()
