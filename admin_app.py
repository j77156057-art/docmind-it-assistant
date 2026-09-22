"""Independent administrative surface for document lifecycle and access control."""
from __future__ import annotations

from contextlib import asynccontextmanager
import csv
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
from typing import Any, Literal
import uuid

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from backend import (
    OIDC_FLOW_COOKIE, OIDC_FLOW_SECONDS, SESSION_COOKIE,
    AppSettings, AuthenticationError, EmbeddingClient, EvaluationError, EvaluationService,
    GovernanceError, HybridRetriever, OIDCAuthenticator, Principal, DocumentSourceStore, build_reranker,
    ModelRouter, ModelRuntime, ModelRuntimeError, QueryDatabase, build_embedding_client,
    configure_logging, log_event, normalize_classification, request_id_context,
)
from backend.metrics import get_metrics
from ingestion import DocumentIngestionService
from backend.artifacts import ARTIFACT_MEDIA_TYPES, ArtifactError, ArtifactService


LOGGER = logging.getLogger("docmind.it.admin")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf", ".docx"}

# Export column order for the procurement-gap admin surfaces. First column is always `id`, last is
# always `created_at`, matching the audit-events export contract (lower_snake_case, utf-8-sig BOM).
CITATION_COLUMNS = [
    "id", "query_id", "citation_kind", "document_chunk_id", "document_version_id",
    "source", "section", "page_number", "line_number", "score", "citation_rank", "created_at",
]
FEEDBACK_COLUMNS = ["id", "query_id", "actor_subject_id", "rating", "comment", "created_at"]
KNOWLEDGE_GAP_COLUMNS = [
    "id", "query_id", "gap_type", "gap_summary", "model_route", "status",
    "resolved_version_id", "created_at", "updated_at",
]
ORG_USER_COLUMNS = [
    "subject_id", "display_name", "email", "status", "last_seen_at",
    "department_key", "created_at",
]
ORG_GROUP_COLUMNS = ["group_key", "display_name", "member_count", "created_at"]
ORG_DEPARTMENT_COLUMNS = ["department_key", "name", "parent_key", "created_at"]


class AclEntryReq(BaseModel):
    principal_type: Literal["user", "group", "role", "department"]
    principal_id: str = Field(min_length=1, max_length=256)


class DepartmentCreateReq(BaseModel):
    department_key: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=256)


class DepartmentMemberReq(BaseModel):
    subject_id: str | None = Field(default=None, max_length=64)
    oidc_sub: str | None = Field(default=None, max_length=256)


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
    response_strategy: Literal["knowledge_first", "generative_first", "hybrid"] | None = None
    api_key: str = Field(default="", max_length=512)


class ReviewDecisionReq(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=1000)
    override: bool = False


class PublishReq(BaseModel):
    comment: str = Field(default="", max_length=1000)
    override: bool = False


class ReasonReq(BaseModel):
    reason: str = Field(min_length=1, max_length=512)


class RetentionPurgeReq(BaseModel):
    stage: Literal["soft", "hard", "both"] = "both"


class KnowledgeGapResolveReq(BaseModel):
    resolved_version_id: int


class EvaluationCaseReq(BaseModel):
    case_key: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=2000)
    expect_refusal: bool = False
    expected_document_key: str = Field(default="", max_length=512)
    expected_heading: str = Field(default="", max_length=512)
    tags: str = Field(default="", max_length=256)
    active: bool = True


class EvaluationRunReq(BaseModel):
    trigger: Literal["manual", "pre_publish", "scheduled"] = "manual"
    document_version_id: int | None = None


def create_admin_app(settings: AppSettings | None = None,
                     embedding_client: EmbeddingClient | None = None,
                     model_runtime: ModelRuntime | None = None,
                     auth_transport: httpx.BaseTransport | None = None) -> FastAPI:
    config = settings or AppSettings.from_environment()
    configure_logging(config)
    database = QueryDatabase(
        config.database_url,
        pool_size=config.database_pool_size,
        max_overflow=config.database_max_overflow,
        pool_timeout=config.database_pool_timeout,
        connect_timeout=config.database_connect_timeout,
        slow_db_ms=config.slow_db_ms,
        secret_key=config.auth_subject_salt.get_secret_value(),
        query_field_key=config.query_field_key.get_secret_value(),
        retention_days=config.retention_days,
        retention_grace_days=config.retention_grace_days,
    )
    embeddings = embedding_client or build_embedding_client(config)
    ingestion = DocumentIngestionService(
        database,
        embeddings,
        max_bytes=config.document_max_bytes,
        chunk_max_chars=config.chunk_max_chars,
        chunk_overlap_chars=config.chunk_overlap_chars,
        chunk_child_max_chars=config.chunk_child_max_chars,
        max_characters=config.document_max_characters,
        max_pages=config.document_max_pages,
        require_review=config.governance_mode == "review",
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
        local_username=config.local_username,
        local_password_hash=config.local_password_hash.get_secret_value(),
        local_display_name=config.local_display_name,
        local_roles=config.local_role_list,
        local_session_hours=config.local_session_hours,
        guest_session_hours=config.guest_session_hours,
        client_id=config.oidc_client_id,
        client_secret=config.oidc_client_secret.get_secret_value(),
        redirect_uri=config.oidc_redirect_uri,
        scopes=config.oidc_scope_list,
        session_hours=config.oidc_session_hours,
        timeout_seconds=config.oidc_timeout_seconds,
        authorization_endpoint=config.oidc_authorization_endpoint,
        token_endpoint=config.oidc_token_endpoint,
        end_session_url=config.oidc_end_session_url,
        http_transport=auth_transport,
    )
    artifacts = ArtifactService(config.artifact_output_path)
    models = ModelRouter.from_settings(
        config, runtime_loader=database.runtime_model_config,
        runtime_credentials_loader=database.runtime_provider_credentials,
    )
    sources = DocumentSourceStore(config.project_root / "data" / "sources")
    runtime = model_runtime or ModelRuntime(timeout_seconds=max(60.0, config.model_timeout_seconds))
    evaluations = EvaluationService(
        settings=config,
        database=database,
        retriever=HybridRetriever(
            database, embeddings, top_k=config.evaluation_top_k,
            reranker=build_reranker(config), rerank_candidate_limit=config.rerank_top_n,
        ),
        embeddings=embeddings,
    )

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
    application.state.evaluations = evaluations

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

    def require_capability(capability: str):
        def authorize(principal: Principal = Depends(authenticated)) -> Principal:
            if not principal.has_capability(capability):
                log_event(LOGGER, logging.WARNING, "admin_authorization_denied", reason=capability)
                raise HTTPException(status_code=403, detail="权限不足")
            return principal
        return authorize

    def governance_override_allowed(principal: Principal, requested: bool) -> bool:
        """Admin override is opt-in twice: by configuration and by an explicit request flag."""
        return bool(
            requested and config.governance_allow_admin_override
            and principal.has_capability("governance.override")
        )

    def governance_http_error(exc: GovernanceError) -> HTTPException:
        if exc.code in {
            "separation_of_duties_violation", "review_approval_required",
            "access_scope_change_requires_acl",
        }:
            return HTTPException(status_code=403, detail=str(exc))
        if exc.code in {
            # State conflicts: the request is authorised but the resource is not in a state that
            # permits the action.
            "invalid_state_transition", "job_already_running", "job_already_active",
            "job_not_cancellable", "evaluation_case_in_use",
        }:
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

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
            duration = round((time.perf_counter() - started) * 1000, 2)
            log_event(
                LOGGER, logging.INFO, "admin_request_completed",
                method=request.method, path=request.url.path,
                status_code=response.status_code, duration_ms=duration,
            )
            if duration >= config.slow_request_ms:
                log_event(
                    LOGGER, logging.WARNING, "slow_admin_request",
                    method=request.method, path=request.url.path,
                    status_code=response.status_code, duration_ms=duration,
                    threshold_ms=config.slow_request_ms,
                )
            metrics = get_metrics()
            metrics.inc_request(request.method, request.url.path, response.status_code)
            metrics.observe_request_duration(duration)
            return response
        finally:
            request_id_context.reset(token)

    @application.get("/")
    async def admin_index():
        return FileResponse(config.admin_index_path)

    @application.get("/login")
    async def login_page():
        return FileResponse(config.project_root / "web" / "login.html")

    @application.get("/assets/login.css")
    async def login_styles():
        return FileResponse(config.project_root / "web" / "login.css", media_type="text/css")

    @application.get("/assets/login.js")
    async def login_script():
        return FileResponse(config.project_root / "web" / "login.js", media_type="text/javascript")

    @application.get("/api/auth/config")
    async def auth_config():
        return {
            "ok": True,
            "mode": config.auth_mode,
            "login_required": config.auth_mode in {"local", "oidc"},
            "local_login": config.auth_mode == "local",
            "sso_login": authenticator.login_enabled,
            "guest_enabled": config.guest_login_enabled and config.auth_mode == "local",
        }

    @application.get("/api/auth/oidc/start")
    async def oidc_start():
        if not authenticator.login_enabled:
            raise HTTPException(status_code=404, detail="未启用 OIDC 登录")
        try:
            authorization = authenticator.begin_authorization()
        except AuthenticationError as exc:
            log_event(LOGGER, logging.WARNING, "admin_oidc_start_failed", reason=exc.code)
            raise HTTPException(status_code=503, detail="身份平台暂时不可用") from None
        response = RedirectResponse(authorization.authorization_url, status_code=302)
        # SameSite=Lax rather than Strict: the callback arrives through a cross-site redirect from
        # the identity provider, and a Strict cookie would not be attached to it. The path scope
        # keeps the transient verifier off every other request.
        response.set_cookie(
            OIDC_FLOW_COOKIE, authenticator.issue_flow_token(authorization), httponly=True,
            samesite="lax", secure=config.environment == "production",
            max_age=OIDC_FLOW_SECONDS, path="/api/auth/oidc",
        )
        return response

    @application.get("/api/auth/oidc/callback")
    async def oidc_callback(request: Request):
        if not authenticator.login_enabled:
            raise HTTPException(status_code=404, detail="未启用 OIDC 登录")
        provider_error = request.query_params.get("error", "")
        if provider_error:
            log_event(LOGGER, logging.WARNING, "admin_oidc_login_rejected",
                      reason=provider_error[:64])
            raise HTTPException(status_code=401, detail="企业登录未通过") from None
        try:
            flow = authenticator.read_flow_token(request.cookies.get(OIDC_FLOW_COOKIE, ""))
            session, principal = authenticator.complete_authorization(
                code=request.query_params.get("code", ""),
                state=request.query_params.get("state", ""), flow=flow,
            )
        except AuthenticationError as exc:
            log_event(LOGGER, logging.WARNING, "admin_oidc_login_failed", reason=exc.code)
            raise HTTPException(status_code=401, detail="登录校验失败，请重新登录") from None
        log_event(LOGGER, logging.INFO, "admin_oidc_login_succeeded",
                  actor_subject_id=principal.subject_id)
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(
            SESSION_COOKIE, session, httponly=True, samesite="strict",
            secure=config.environment == "production",
            max_age=config.oidc_session_hours * 3600,
        )
        response.delete_cookie(OIDC_FLOW_COOKIE, path="/api/auth/oidc")
        return response

    @application.post("/api/auth/login")
    async def auth_login(payload: dict):
        if config.auth_mode != "local":
            raise HTTPException(status_code=400, detail="当前认证模式不使用本地登录")
        try:
            token = authenticator.login(str(payload.get("username") or ""), str(payload.get("password") or ""))
        except AuthenticationError:
            raise HTTPException(status_code=401, detail="用户名或密码错误") from None
        response = JSONResponse({"ok": True})
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict",
                            secure=config.environment == "production", max_age=config.local_session_hours * 3600)
        return response

    @application.post("/api/auth/guest")
    async def auth_guest(request: Request):
        if config.auth_mode != "local" or not config.guest_login_enabled:
            raise HTTPException(status_code=403, detail="游客登录未启用")
        query_url = f"{request.url.scheme}://{request.url.hostname}:{config.port}/"
        response = JSONResponse({"ok": True, "redirect": query_url})
        response.set_cookie(
            SESSION_COOKIE, authenticator.guest_login(), httponly=True,
            samesite="strict", secure=config.environment == "production",
            max_age=config.guest_session_hours * 3600,
        )
        return response

    @application.post("/api/auth/logout")
    async def auth_logout():
        response = JSONResponse({"ok": True, "redirect": authenticator.logout_url()})
        response.delete_cookie(SESSION_COOKIE)
        return response

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
        queue = database.ingestion_queue_stats(
            timeout_seconds=config.ingestion_job_timeout_seconds,
        )
        # A stalled queue is only a problem when this service is supposed to enqueue work:
        # jobs that sit past the job timeout mean no worker is consuming them.
        queue_stalled = config.ingestion_worker_enabled and bool(
            queue["stale_queued"] or queue["stale_running"]
        )
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
            {
                "ok": ready,
                "status": "ready" if ready else "not_ready",
                "checks": checks,
                # Diagnostics, deliberately outside `checks`: a stalled queue means "no worker is
                # consuming jobs", which a restart cannot fix, so it must not fail readiness and
                # trigger pod restarts. Alerting watches this field.
                "ingestion": {
                    "worker_enabled": config.ingestion_worker_enabled,
                    "stalled": queue_stalled,
                    "queued": queue["queued"],
                    "running": queue["running"],
                    "failed": queue["failed"],
                    "stale": queue["stale_queued"] + queue["stale_running"],
                },
            },
            status_code=200 if ready else 503,
        )

    @application.get("/api/me")
    async def me(principal: Principal = Depends(require_capability("document.read"))):
        return {
            "ok": True,
            "subject_id": principal.subject_id,
            "display_name": principal.display_name,
            "roles": sorted(principal.roles),
            "groups": sorted(principal.groups),
            "capabilities": sorted(principal.capabilities),
            "governance_mode": config.governance_mode,
            "evaluation_gate_mode": config.evaluation_gate_mode,
            "ingestion_worker_enabled": config.ingestion_worker_enabled,
            "ingestion_engine": config.ingestion_engine,
            "governance_require_separation_of_duties":
                config.governance_require_separation_of_duties,
            "governance_override_allowed": config.governance_allow_admin_override
            and principal.has_capability("governance.override"),
            # Bypassing a blocking evaluation gate is its own policy: the switch, the publish
            # capability and the override capability must all be present.
            "evaluation_override_allowed": config.evaluation_allow_override
            and principal.has_capability("governance.override")
            and principal.has_capability("document.publish"),
        }

    @application.get("/api/admin/documents")
    async def documents(_principal: Principal = Depends(require_capability("document.read"))):
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
                              principal: Principal = Depends(require_capability("document.read"))):
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
    async def document_acl(document_id: int,
                           _principal: Principal = Depends(require_capability("document.read"))):
        try:
            return {"ok": True, **database.document_access(document_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @application.put("/api/admin/documents/{document_id}/acl")
    async def replace_document_acl(document_id: int, payload: DocumentAclReq,
                                   principal: Principal = Depends(require_capability("acl.write"))):
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

    @application.get("/api/admin/governance/pending")
    async def governance_pending(limit: int = Query(100, ge=1, le=200),
                                 _principal: Principal = Depends(
                                     require_capability("document.review"))):
        return {"ok": True, "items": database.pending_review_versions(limit)}

    @application.get("/api/admin/documents/{document_id}/versions/{version}/reviews")
    async def version_reviews(document_id: int, version: int,
                              _principal: Principal = Depends(
                                  require_capability("document.read"))):
        try:
            items = database.document_version_reviews(document_id, version)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {"ok": True, "items": items}

    @application.get("/api/admin/documents/{document_id}/versions/{version}/preview")
    async def version_preview(document_id: int, version: int,
                              limit: int = Query(200, ge=1, le=500),
                              principal: Principal = Depends(
                                  require_capability("document.review"))):
        """Reviewer preview. Bypasses document ACL, so every read is audited."""
        try:
            payload = database.document_version_chunks(document_id, version, limit)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="document_version_preview",
            target_type="document",
            target_ref=f"{document_id}:v{version}",
            result="success",
            request_id=request_id_context.get(),
        )
        return {"ok": True, **payload}

    @application.post("/api/admin/documents/{document_id}/versions/{version}/review")
    async def review_version(document_id: int, version: int, payload: ReviewDecisionReq,
                             principal: Principal = Depends(
                                 require_capability("document.review"))):
        try:
            state = database.review_document_version(
                document_id=document_id,
                version=version,
                decision=payload.decision,
                actor_subject_id=principal.subject_id,
                comment=payload.comment,
                request_id=request_id_context.get(),
                require_separation_of_duties=config.governance_require_separation_of_duties,
                allow_override=governance_override_allowed(principal, payload.override),
            )
        except GovernanceError as exc:
            database.record_audit_event(
                actor_subject_id=principal.subject_id,
                action=f"document_review_{payload.decision}",
                target_type="document",
                target_ref=f"{document_id}:v{version}",
                result="failed",
                request_id=request_id_context.get(),
            )
            raise governance_http_error(exc) from None
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action=f"document_review_{payload.decision}",
            target_type="document",
            target_ref=f"{document_id}:v{version}",
            result="success",
            request_id=request_id_context.get(),
        )
        return {"ok": True, "version": state}

    @application.post("/api/admin/documents/{document_id}/versions/{version}/publish")
    async def publish_version(document_id: int, version: int, payload: PublishReq,
                              principal: Principal = Depends(
                                  require_capability("document.publish"))):
        request_id = request_id_context.get()
        # Bypassing the quality gate needs the publish capability AND the override capability, plus
        # its own configuration switch: being allowed to publish is not the same as being allowed
        # to ignore measurements, and that policy is independent of separation of duties.
        gate_override = bool(
            payload.override and config.evaluation_allow_override
            and principal.has_capability("governance.override")
            and principal.has_capability("document.publish")
        )
        gate = None
        if config.evaluation_gate_mode != "off":
            try:
                version_pk = database.document_version_pk(document_id, version)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None
            try:
                gate = await run_in_threadpool(
                    evaluations.run,
                    trigger="pre_publish",
                    document_version_id=version_pk,
                    actor_subject_id=principal.subject_id,
                    request_id=request_id,
                )
            except EvaluationError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"评测执行失败：{exc.detail or exc.code}",
                ) from None
            if gate.get("gate_result") == "block" and not gate_override:
                database.record_audit_event(
                    actor_subject_id=principal.subject_id,
                    action="document_publish",
                    target_type="document",
                    target_ref=f"{document_id}:v{version}",
                    result="failed",
                    request_id=request_id,
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"评测门未通过，发布被阻断：{gate.get('gate_reason') or '指标不达标'}",
                )
        try:
            state = database.publish_document_version(
                document_id=document_id,
                version=version,
                actor_subject_id=principal.subject_id,
                comment=payload.comment,
                request_id=request_id,
                allow_override=governance_override_allowed(principal, payload.override),
                gate_override=bool(gate_override and gate and gate.get("gate_result") == "block"),
                gate_comment=(
                    f"评测门未通过仍发布：{gate.get('gate_reason') or ''}"
                    if gate and gate.get("gate_result") == "block" else ""
                ),
            )
        except GovernanceError as exc:
            raise governance_http_error(exc) from None
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="document_publish",
            target_type="document",
            target_ref=f"{document_id}:v{version}",
            result="success",
            request_id=request_id,
        )
        return {"ok": True, "version": state, "evaluation": gate}

    @application.post("/api/admin/documents/{document_id}/versions/{version}/withdraw")
    async def withdraw_version(document_id: int, version: int, payload: ReasonReq,
                               principal: Principal = Depends(
                                   require_capability("document.withdraw"))):
        try:
            state = database.withdraw_document_version(
                document_id=document_id,
                version=version,
                actor_subject_id=principal.subject_id,
                reason=payload.reason,
                request_id=request_id_context.get(),
            )
        except GovernanceError as exc:
            raise governance_http_error(exc) from None
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="document_withdraw",
            target_type="document",
            target_ref=f"{document_id}:v{version}",
            result="success",
            request_id=request_id_context.get(),
        )
        return {"ok": True, "version": state}

    @application.post("/api/admin/documents/{document_id}/versions/{version}/rollback")
    async def rollback_version(document_id: int, version: int, payload: ReasonReq,
                               principal: Principal = Depends(
                                   require_capability("document.rollback"))):
        try:
            state = database.rollback_document_version(
                document_id=document_id,
                version=version,
                actor_subject_id=principal.subject_id,
                reason=payload.reason,
                request_id=request_id_context.get(),
            )
        except GovernanceError as exc:
            raise governance_http_error(exc) from None
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="document_rollback",
            target_type="document",
            target_ref=f"{document_id}:v{version}",
            result="success",
            request_id=request_id_context.get(),
        )
        return {"ok": True, "version": state}

    @application.get("/api/admin/ingestion/jobs")
    async def ingestion_jobs(status: str = Query("", max_length=16),
                             limit: int = Query(50, ge=1, le=200),
                             _principal: Principal = Depends(
                                 require_capability("document.read"))):
        try:
            items = database.list_ingestion_jobs(limit=limit, status=status or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {
            "ok": True,
            "items": items,
            "worker_enabled": config.ingestion_worker_enabled,
            "queue": database.ingestion_queue_stats(
                timeout_seconds=config.ingestion_job_timeout_seconds,
            ),
        }

    @application.post("/api/admin/ingestion/jobs/{job_id}/retry")
    async def retry_ingestion_job(job_id: int,
                                  principal: Principal = Depends(
                                      require_capability("document.write"))):
        try:
            job = database.retry_ingestion_job(job_id, actor_subject_id=principal.subject_id)
        except GovernanceError as exc:
            raise governance_http_error(exc) from None
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="ingestion_job_retry",
            target_type="ingestion_job",
            target_ref=str(job_id),
            result="success",
            request_id=request_id_context.get(),
        )
        return {"ok": True, "job": job}

    @application.post("/api/admin/ingestion/jobs/{job_id}/cancel")
    async def cancel_ingestion_job(job_id: int,
                                   principal: Principal = Depends(
                                       require_capability("document.write"))):
        try:
            job = database.cancel_ingestion_job(job_id, actor_subject_id=principal.subject_id)
        except GovernanceError as exc:
            raise governance_http_error(exc) from None
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="ingestion_job_cancel",
            target_type="ingestion_job",
            target_ref=str(job_id),
            result="success",
            request_id=request_id_context.get(),
        )
        return {"ok": True, "job": job}

    @application.get("/api/admin/evaluation/cases")
    async def evaluation_cases(limit: int = Query(200, ge=1, le=500),
                               _principal: Principal = Depends(
                                   require_capability("document.read"))):
        return {
            "ok": True,
            "items": database.list_evaluation_cases(limit=limit),
            "gate_mode": config.evaluation_gate_mode,
            "thresholds": {
                "min_recall": config.evaluation_min_recall,
                "min_citation_accuracy": config.evaluation_min_citation_accuracy,
                "max_regression": config.evaluation_max_regression,
                "top_k": config.evaluation_top_k,
            },
        }

    @application.put("/api/admin/evaluation/cases")
    async def save_evaluation_case(payload: EvaluationCaseReq,
                                   principal: Principal = Depends(
                                       require_capability("evaluation.run"))):
        try:
            case = database.upsert_evaluation_case(
                case_key=payload.case_key,
                question=payload.question,
                expect_refusal=payload.expect_refusal,
                expected_document_key=payload.expected_document_key or None,
                expected_heading=payload.expected_heading or None,
                tags=payload.tags,
                active=payload.active,
                actor_subject_id=principal.subject_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="evaluation_case_save",
            target_type="evaluation_case",
            target_ref=case["case_key"],
            result="success",
            request_id=request_id_context.get(),
        )
        return {"ok": True, "case": case}

    @application.delete("/api/admin/evaluation/cases/{case_id}")
    async def remove_evaluation_case(case_id: int,
                                     principal: Principal = Depends(
                                         require_capability("evaluation.run"))):
        try:
            database.delete_evaluation_case(case_id)
        except GovernanceError as exc:
            raise governance_http_error(exc) from None
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="evaluation_case_delete",
            target_type="evaluation_case",
            target_ref=str(case_id),
            result="success",
            request_id=request_id_context.get(),
        )
        return {"ok": True}

    @application.post("/api/admin/evaluation/runs")
    async def create_evaluation_run(payload: EvaluationRunReq,
                                    principal: Principal = Depends(
                                        require_capability("evaluation.run"))):
        request_id = request_id_context.get()
        if (
            payload.trigger == "pre_publish"
            and payload.document_version_id is None
        ):
            raise HTTPException(status_code=400, detail="发布前评测必须指定文档版本")
        try:
            run = await run_in_threadpool(
                evaluations.run,
                trigger=payload.trigger,
                document_version_id=payload.document_version_id,
                actor_subject_id=principal.subject_id,
                request_id=request_id,
            )
        except EvaluationError as exc:
            raise HTTPException(
                status_code=409, detail=f"评测执行失败：{exc.detail or exc.code}",
            ) from None
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="evaluation_run",
            target_type="evaluation_run",
            target_ref=str(run["run_id"]),
            result="success",
            request_id=request_id,
        )
        return {"ok": True, "run": run}

    @application.get("/api/admin/evaluation/runs")
    async def evaluation_runs(limit: int = Query(50, ge=1, le=200),
                              document_version_id: int | None = Query(None),
                              _principal: Principal = Depends(
                                  require_capability("document.read"))):
        return {
            "ok": True,
            "items": database.list_evaluation_runs(
                limit=limit, document_version_id=document_version_id,
            ),
        }

    @application.get("/api/admin/evaluation/runs/{run_id}")
    async def evaluation_run_detail(run_id: int,
                                    _principal: Principal = Depends(
                                        require_capability("document.read"))):
        try:
            detail = database.evaluation_run_detail(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {"ok": True, "run": detail}

    @application.get("/api/admin/audit-events")
    async def audit_events(limit: int = 100,
                           _principal: Principal = Depends(require_capability("audit.read"))):
        return {"ok": True, "items": database.audit_events(limit)}

    @application.get("/api/admin/audit-events/export")
    async def audit_events_export(
        export_format: str = "csv",
        start: datetime | None = None,
        end: datetime | None = None,
        action: str | None = None,
        target_type: str | None = None,
        actor: str | None = None,
        limit: int = 1000,
        principal: Principal = Depends(require_capability("audit.read")),
    ):
        if export_format not in {"csv", "json"}:
            raise HTTPException(status_code=400, detail="export_format 仅支持 csv 或 json")
        rows = database.audit_events_export(
            start=start, end=end, action=action, target_type=target_type,
            actor=actor, limit=limit,
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if export_format == "json":
            content = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
            media_type = "application/json"
            filename = f"audit-export-{stamp}.json"
        else:
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(
                ["id", "created_at", "actor_subject_id", "action",
                 "target_type", "target_ref", "result", "request_id"],
            )
            for row in rows:
                writer.writerow([
                    row["id"], row["created_at"], row["actor_subject_id"], row["action"],
                    row["target_type"], row["target_ref"], row["result"], row["request_id"],
                ])
            # BOM so Excel opens UTF-8 CSV with CJK intact.
            content = buffer.getvalue().encode("utf-8-sig")
            media_type = "text/csv; charset=utf-8"
            filename = f"audit-export-{stamp}.csv"
        summary = f"format={export_format};rows={len(rows)}"
        if action:
            summary += f";action={action}"
        if target_type:
            summary += f";target_type={target_type}"
        if actor:
            summary += f";actor={actor}"
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="audit.export",
            target_type="audit_export",
            target_ref=summary[:512],
            result="success",
            request_id=request_id_context.get() or "",
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    def _export_rows(rows, columns, *, export_format: str, entity: str, action: str,
                     target_type: str, principal: Principal, summary_extra: str = "") -> Response:
        """Render `rows` as CSV (utf-8-sig) or JSON and self-audit the download.

        Reuses the audit-events export contract: `id`-first / `created_at`-last columns, BOM for
        Excel CJK, and a `<entity>.export` audit row for every download.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if export_format == "json":
            content = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
            media_type = "application/json"
            filename = f"{entity}-export-{stamp}.json"
        else:
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([row.get(col) for col in columns])
            # BOM so Excel opens UTF-8 CSV with CJK intact.
            content = buffer.getvalue().encode("utf-8-sig")
            media_type = "text/csv; charset=utf-8"
            filename = f"{entity}-export-{stamp}.csv"
        summary = f"format={export_format};rows={len(rows)}{summary_extra}"
        database.record_audit_event(
            actor_subject_id=principal.subject_id, action=action,
            target_type=target_type, target_ref=summary[:512],
            result="success", request_id=request_id_context.get() or "",
        )
        return Response(
            content=content, media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @application.get("/api/admin/citations")
    async def admin_citations(
        limit: int = 200,
        _principal: Principal = Depends(require_capability("audit.read"))):
        """List persisted answer citations."""
        return {"ok": True, "items": database.citations(limit=limit)}

    @application.get("/api/admin/citations/export")
    async def admin_citations_export(
        export_format: str = "csv", start: datetime | None = None, end: datetime | None = None,
        query_id: int | None = None, limit: int = 2000,
        principal: Principal = Depends(require_capability("audit.read"))):
        if export_format not in {"csv", "json"}:
            raise HTTPException(status_code=400, detail="export_format 仅支持 csv 或 json")
        rows = database.citations_export(start=start, end=end, query_id=query_id, limit=limit)
        return _export_rows(
            rows, CITATION_COLUMNS, export_format=export_format, entity="citations",
            action="citations.export", target_type="citations_export", principal=principal,
        )

    @application.get("/api/admin/feedback")
    async def admin_feedback(
        rating: str | None = None, actor: str | None = None, limit: int = 200,
        _principal: Principal = Depends(require_capability("audit.read"))):
        """List user feedback."""
        return {"ok": True, "items": database.feedback(rating=rating, actor=actor, limit=limit)}

    @application.get("/api/admin/feedback/export")
    async def admin_feedback_export(
        export_format: str = "csv", rating: str | None = None, actor: str | None = None,
        limit: int = 2000, principal: Principal = Depends(require_capability("audit.read"))):
        if export_format not in {"csv", "json"}:
            raise HTTPException(status_code=400, detail="export_format 仅支持 csv 或 json")
        rows = database.feedback_export(rating=rating, actor=actor, limit=limit)
        return _export_rows(
            rows, FEEDBACK_COLUMNS, export_format=export_format, entity="feedback",
            action="feedback.export", target_type="feedback_export", principal=principal,
        )

    @application.get("/api/admin/knowledge-gaps")
    async def admin_knowledge_gaps(
        gap_type: str | None = None, status: str | None = None, limit: int = 200,
        _principal: Principal = Depends(require_capability("audit.read"))):
        """List auto-registered knowledge gaps."""
        return {"ok": True, "items": database.knowledge_gaps(gap_type=gap_type, status=status, limit=limit)}

    @application.get("/api/admin/knowledge-gaps/export")
    async def admin_knowledge_gaps_export(
        export_format: str = "csv", gap_type: str | None = None, status: str | None = None,
        limit: int = 2000, principal: Principal = Depends(require_capability("audit.read"))):
        if export_format not in {"csv", "json"}:
            raise HTTPException(status_code=400, detail="export_format 仅支持 csv 或 json")
        rows = database.knowledge_gaps_export(gap_type=gap_type, status=status, limit=limit)
        return _export_rows(
            rows, KNOWLEDGE_GAP_COLUMNS, export_format=export_format, entity="knowledge-gaps",
            action="knowledge_gaps.export", target_type="knowledge_gaps_export",
            principal=principal,
        )

    @application.post("/api/admin/knowledge-gaps/{gap_id}/resolve")
    async def admin_resolve_knowledge_gap(
        gap_id: int, payload: KnowledgeGapResolveReq,
        principal: Principal = Depends(require_capability("document.write"))):
        """Close a gap as addressed, linking the version that filled it. Self-audited."""
        try:
            database.resolve_knowledge_gap(
                gap_id, payload.resolved_version_id, principal.subject_id,
                request_id_context.get() or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {"ok": True}

    @application.post("/api/admin/knowledge-gaps/{gap_id}/dismiss")
    async def admin_dismiss_knowledge_gap(
        gap_id: int, principal: Principal = Depends(require_capability("document.write"))):
        """Dismiss a gap. Self-audited."""
        try:
            database.dismiss_knowledge_gap(
                gap_id, principal.subject_id, request_id_context.get() or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {"ok": True}

    @application.get("/api/admin/org/users")
    async def admin_org_users(
        limit: int = 500, _principal: Principal = Depends(require_capability("audit.read"))):
        """List org users (read-only view over the lazily-synced org model)."""
        return {"ok": True, "items": database.org_users(limit=limit)}

    @application.get("/api/admin/org/groups")
    async def admin_org_groups(
        limit: int = 500, _principal: Principal = Depends(require_capability("audit.read"))):
        """List org groups with member counts."""
        return {"ok": True, "items": database.org_groups(limit=limit)}

    @application.get("/api/admin/org/departments")
    async def admin_org_departments(
        limit: int = 500, _principal: Principal = Depends(require_capability("audit.read"))):
        """List org departments (metadata only)."""
        return {"ok": True, "items": database.org_departments(limit=limit)}

    @application.get("/api/admin/org/users/export")
    async def admin_org_users_export(
        export_format: str = "csv", limit: int = 2000,
        principal: Principal = Depends(require_capability("audit.read"))):
        if export_format not in {"csv", "json"}:
            raise HTTPException(status_code=400, detail="export_format 仅支持 csv 或 json")
        rows = database.org_users_export(limit=limit)
        return _export_rows(
            rows, ORG_USER_COLUMNS, export_format=export_format, entity="org-users",
            action="org_users.export", target_type="org_users_export", principal=principal,
        )

    @application.get("/api/admin/org/groups/export")
    async def admin_org_groups_export(
        export_format: str = "csv", limit: int = 2000,
        principal: Principal = Depends(require_capability("audit.read"))):
        if export_format not in {"csv", "json"}:
            raise HTTPException(status_code=400, detail="export_format 仅支持 csv 或 json")
        rows = database.org_groups_export(limit=limit)
        return _export_rows(
            rows, ORG_GROUP_COLUMNS, export_format=export_format, entity="org-groups",
            action="org_groups.export", target_type="org_groups_export", principal=principal,
        )

    @application.get("/api/admin/org/departments/export")
    async def admin_org_departments_export(
        export_format: str = "csv", limit: int = 2000,
        principal: Principal = Depends(require_capability("audit.read"))):
        if export_format not in {"csv", "json"}:
            raise HTTPException(status_code=400, detail="export_format 仅支持 csv 或 json")
        rows = database.org_departments_export(limit=limit)
        return _export_rows(
            rows, ORG_DEPARTMENT_COLUMNS, export_format=export_format, entity="org-departments",
            action="org_departments.export", target_type="org_departments_export",
            principal=principal,
        )

    @application.post("/api/admin/org/departments")
    async def admin_org_department_create(
        payload: DepartmentCreateReq,
        principal: Principal = Depends(require_capability("acl.write"))):
        """Create (upsert) a department. Departments decide document visibility via the
        ``department`` ACL principal type, so this write end shares the ``acl.write`` capability
        with ``replace_document_acl``."""
        try:
            database.create_department(
                payload.department_key, payload.name,
                actor_subject_id=principal.subject_id,
                request_id=request_id_context.get() or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"ok": True, "department_key": payload.department_key}

    @application.delete("/api/admin/org/departments/{department_key}")
    async def admin_org_department_delete(
        department_key: str,
        principal: Principal = Depends(require_capability("acl.write"))):
        """Delete a department; its members are removed (FK cascade on PG, explicit on SQLite)."""
        try:
            database.delete_department(
                department_key,
                actor_subject_id=principal.subject_id,
                request_id=request_id_context.get() or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"ok": True, "department_key": department_key}

    @application.post("/api/admin/org/departments/{department_key}/members")
    async def admin_org_department_add_member(
        department_key: str, payload: DepartmentMemberReq,
        principal: Principal = Depends(require_capability("acl.write"))):
        """Add a user to a department. Identify the member by ``subject_id`` or ``oidc_sub``
        (subject_id takes precedence; oidc_sub is resolved to a subject_id)."""
        try:
            database.add_user_to_department(
                department_key, subject_id=payload.subject_id, oidc_sub=payload.oidc_sub,
                actor_subject_id=principal.subject_id,
                request_id=request_id_context.get() or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"ok": True, "department_key": department_key}

    @application.delete("/api/admin/org/departments/{department_key}/members/{subject_id}")
    async def admin_org_department_remove_member(
        department_key: str, subject_id: str,
        principal: Principal = Depends(require_capability("acl.write"))):
        """Remove a user from a department."""
        try:
            database.remove_user_from_department(
                department_key, subject_id,
                actor_subject_id=principal.subject_id,
                request_id=request_id_context.get() or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"ok": True, "department_key": department_key, "subject_id": subject_id}

    @application.get("/api/admin/retention/preview")
    async def retention_preview(
        principal: Principal = Depends(require_capability("audit.read")),
    ):
        """Show how many documents the next purge would touch, without changing anything.

        `soft_due` are inside the retention window but old enough to be soft-marked; `hard_due` are
        past the grace window and would be physically removed. The read is itself audited.
        """
        preview = database.retention_preview()
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="retention.preview",
            target_type="retention",
            target_ref=(
                f"total={preview['total_documents']};"
                f"soft_due={preview['soft_due']};hard_due={preview['hard_due']}"
            ),
            result="success",
            request_id=request_id_context.get() or "",
        )
        return {"ok": True, **preview}

    @application.post("/api/admin/retention/purge")
    async def retention_purge(
        payload: RetentionPurgeReq,
        principal: Principal = Depends(require_capability("document.write")),
    ):
        """Apply the retention policy (先软后硬). Destructive, so it needs `document.write`.

        `stage` is "both" (default), "soft" (only mark) or "hard" (only remove past grace). The
        action and its outcome counts are recorded for audit.
        """
        try:
            result = database.retention_purge(stage=payload.stage)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        summary = (
            f"stage={payload.stage};soft_marked={result['soft_marked']};"
            f"hard_deleted={result['hard_deleted']}"
        )
        database.record_audit_event(
            actor_subject_id=principal.subject_id,
            action="retention.purge",
            target_type="retention",
            target_ref=summary[:512],
            result="success",
            request_id=request_id_context.get() or "",
        )
        return {"ok": True, **result}

    @application.get("/api/admin/metrics")
    async def metrics(_principal: Principal = Depends(require_capability("audit.read"))):
        """Process-local observability snapshot: request counts/latency, queue depth, model spend."""
        snapshot = get_metrics().snapshot()
        snapshot["ingestion_queue_depth"] = database.count_ingestion_jobs(status="queued")
        snapshot["ingestion_jobs_failed"] = database.count_ingestion_jobs(status="failed")
        snapshot["model_usage"] = database.model_usage_totals()
        return {"ok": True, **snapshot}

    @application.get("/api/admin/model-config")
    async def model_config(_principal: Principal = Depends(require_capability("usage.read"))):
        active = models.status()
        runtime_status = await run_in_threadpool(
            runtime.status, active, models.base_url({**active, "route": active["mode"]}),
            models.credential(active["provider"]),
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
                                  principal: Principal = Depends(require_capability("model.write"))):
        response_strategy = payload.response_strategy or models.response_strategy()
        target_ref = f"{payload.mode}:{payload.provider}:{payload.model}:{response_strategy}"
        try:
            selected = models.validate_selection(
                payload.mode, payload.provider, payload.model, payload.api_key,
            )
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
                payload.api_key.strip() or models.credential(selected["provider"]),
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
            response_strategy=response_strategy,
            actor_subject_id=principal.subject_id,
            request_id=request_id_context.get(),
        )
        if payload.api_key.strip() and selected["provider"] not in {"builtin", "ollama", "llamacpp"}:
            database.set_runtime_provider_credential(
                provider=selected["provider"], api_key=payload.api_key.strip(),
                actor_subject_id=principal.subject_id, request_id=request_id_context.get(),
            )
        return {
            "ok": True, "active": models.status(), "runtime": runtime_status,
            "override": saved,
        }

    @application.get("/api/admin/artifacts")
    async def list_artifacts(_principal: Principal = Depends(require_capability("artifact.read"))):
        return {"ok": True, "items": await run_in_threadpool(artifacts.list)}

    @application.post("/api/admin/artifacts")
    async def create_artifact(payload: ArtifactCreateReq,
                              principal: Principal = Depends(require_capability("artifact.write"))):
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
    async def download_artifact(filename: str,
                                _principal: Principal = Depends(require_capability("artifact.read"))):
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
        principal: Principal = Depends(require_capability("document.write")),
    ):
        filename = Path(file.filename or "document").name
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise HTTPException(status_code=400, detail="仅支持 Markdown、TXT、PDF 和 DOCX")
        if access_scope not in {"public", "restricted"}:
            raise HTTPException(status_code=400, detail="文档访问范围无效")
        # Validated here as well as in the repository, so a bad label is a 400 for the operator
        # instead of a 500 from deeper down. Empty means "leave it alone" on a re-import.
        if classification.strip():
            try:
                classification = normalize_classification(classification)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
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
                    ingestion.register_version if config.ingestion_worker_enabled
                    else ingestion.import_file,
                    target,
                    title=title.strip() or Path(filename).stem,
                    source_key=source,
                    access_scope=access_scope,
                    classification=classification,
                    submitted_by_subject_id=principal.subject_id,
                    request_id=request_id,
                )
                await run_in_threadpool(
                    sources.store,
                    target,
                    document_id=result["document_id"],
                    version=result["version"],
                    filename=filename,
                )
                if not config.ingestion_worker_enabled:
                    database.record_audit_event(
                        actor_subject_id=principal.subject_id,
                        action="document_import",
                        target_type="document",
                        target_ref=source,
                        result="success",
                        request_id=request_id,
                    )
                    return {
                        "ok": True,
                        **result,
                        "governance_mode": config.governance_mode,
                        "review_required": result.get("status") == "staged",
                    }
                # Asynchronous path: the upload is durable and the version exists, so the request
                # can return immediately; indexing happens in the worker.
                job = None
                if not result["duplicate"]:
                    try:
                        job = await run_in_threadpool(
                            database.enqueue_ingestion_job,
                            job_type="import",
                            document_id=result["document_id"],
                            version_id=result["version_id"],
                            payload={"filename": filename, "version": result["version"]},
                            created_by_subject_id=principal.subject_id,
                            request_id=request_id,
                            max_attempts=config.ingestion_max_attempts,
                        )
                    except Exception:
                        # Never leave a version sitting in `queued` with no job behind it.
                        await run_in_threadpool(
                            database.fail_document_import, result["version_id"], "enqueue_failed",
                        )
                        raise
                database.record_audit_event(
                    actor_subject_id=principal.subject_id,
                    action="document_import",
                    target_type="document",
                    target_ref=source,
                    result="success",
                    request_id=request_id,
                )
                return JSONResponse(
                    {
                        "ok": True,
                        **result,
                        "queued": job is not None,
                        "job_id": job["job_id"] if job else None,
                        "status": "queued" if job else result.get("status"),
                        # Predicted from this service's configuration; the worker applies the same
                        # setting and the document list shows the authoritative result.
                        "governance_mode": config.governance_mode,
                        "review_required": config.governance_mode == "review",
                    },
                    status_code=202 if job else 200,
                )
        except GovernanceError as exc:
            database.record_audit_event(
                actor_subject_id=principal.subject_id,
                action="document_import",
                target_type="document",
                target_ref=source,
                result="failed",
                request_id=request_id,
            )
            raise governance_http_error(exc) from None
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
