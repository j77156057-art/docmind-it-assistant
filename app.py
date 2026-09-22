from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import re
import time
import uuid

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from assistant import ITQueryService
from backend import (
    OIDC_FLOW_COOKIE, OIDC_FLOW_SECONDS, SESSION_COOKIE,
    AppSettings, AuthenticationError, EmbeddingClient, HybridRetriever, ModelGateway, build_reranker,
    ModelGatewayError, ModelRouter, OIDCAuthenticator, Principal, QueryDatabase,
    build_embedding_client, configure_logging, log_event, request_id_context,
)
from backend.metrics import get_metrics
from backend.ratelimit import QueryRateLimiter
from backend.db_models import FEEDBACK_RATING


LOGGER = logging.getLogger("docmind.it")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class QueryReq(BaseModel):
    session_id: str = "default"
    question: str = ""


class FeedbackReq(BaseModel):
    query_id: int
    rating: str
    comment: str = ""


def create_app(settings: AppSettings | None = None, model_gateway: ModelGateway | None = None,
               embedding_client: EmbeddingClient | None = None,
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
    models = ModelRouter.from_settings(
        config, runtime_loader=database.runtime_model_config,
        runtime_credentials_loader=database.runtime_provider_credentials,
    )
    gateway = model_gateway or ModelGateway(
        timeout_seconds=config.model_timeout_seconds,
        max_retries=config.model_max_retries,
        retry_backoff_seconds=config.model_retry_backoff_seconds,
        max_output_tokens=config.model_max_output_tokens,
        temperature=config.model_temperature,
    )
    embeddings = embedding_client or build_embedding_client(config)
    retriever = HybridRetriever(
        database, embeddings, top_k=config.retrieval_top_k,
        reranker=build_reranker(config), rerank_candidate_limit=config.rerank_top_n,
    )
    service = ITQueryService(str(config.knowledge_path), database, models, gateway, retriever)
    rate_limiter = QueryRateLimiter(config.query_daily_quota)
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

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        database.initialize()
        status = models.status()
        log_event(
            LOGGER, logging.INFO, "application_started",
            environment=config.environment, provider=status["provider"], model=status["model"],
        )
        yield
        database.dispose()
        log_event(LOGGER, logging.INFO, "application_stopped", environment=config.environment)

    application = FastAPI(title=config.app_name, lifespan=lifespan)
    application.state.settings = config
    application.state.database = database
    application.state.models = models
    application.state.gateway = gateway
    application.state.embeddings = embeddings
    application.state.retriever = retriever
    application.state.service = service
    application.state.authenticator = authenticator

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
            log_event(LOGGER, logging.WARNING, "authentication_failed", reason=exc.code)
            raise HTTPException(
                status_code=401,
                detail="未认证或登录已失效",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None

    def require_role(role: str):
        def authorize(principal: Principal = Depends(authenticated)) -> Principal:
            if not principal.allows(role):
                log_event(
                    LOGGER, logging.WARNING, "authorization_denied", required_role=role,
                    actor_subject_id=principal.subject_id,
                )
                raise HTTPException(status_code=403, detail="权限不足")
            return principal
        return authorize

    viewer = require_role("viewer")
    auditor = require_role("auditor")

    @application.middleware("http")
    async def request_logging(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "").strip()
        request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else uuid.uuid4().hex
        context_token = request_id_context.set(request_id)
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001 - privacy-safe boundary response
                duration = round((time.perf_counter() - started) * 1000, 2)
                log_event(
                    LOGGER, logging.ERROR, "request_failed", method=request.method,
                    path=request.url.path, status_code=500, duration_ms=duration,
                    error_type=type(exc).__name__,
                )
                response = JSONResponse(
                    {"ok": False, "error": "服务内部错误", "request_id": request_id},
                    status_code=500,
                )
            response.headers["X-Request-ID"] = request_id
            duration = round((time.perf_counter() - started) * 1000, 2)
            log_event(
                LOGGER, logging.INFO, "request_completed", method=request.method,
                path=request.url.path, status_code=response.status_code, duration_ms=duration,
            )
            metrics = get_metrics()
            metrics.inc_request(request.method, request.url.path, response.status_code)
            metrics.observe_request_duration(duration)
            if duration >= config.slow_request_ms:
                log_event(
                    LOGGER, logging.WARNING, "slow_request",
                    method=request.method, path=request.url.path,
                    status_code=response.status_code, duration_ms=duration,
                    threshold_ms=config.slow_request_ms,
                )
            return response
        finally:
            request_id_context.reset(context_token)

    @application.get("/")
    async def index():
        return FileResponse(config.web_index_path)

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
            log_event(LOGGER, logging.WARNING, "oidc_start_failed", reason=exc.code)
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
            log_event(LOGGER, logging.WARNING, "oidc_login_rejected", reason=provider_error[:64])
            raise HTTPException(status_code=401, detail="企业登录未通过") from None
        try:
            flow = authenticator.read_flow_token(request.cookies.get(OIDC_FLOW_COOKIE, ""))
            session, principal = authenticator.complete_authorization(
                code=request.query_params.get("code", ""),
                state=request.query_params.get("state", ""), flow=flow,
            )
        except AuthenticationError as exc:
            log_event(LOGGER, logging.WARNING, "oidc_login_failed", reason=exc.code)
            raise HTTPException(status_code=401, detail="登录校验失败，请重新登录") from None
        log_event(LOGGER, logging.INFO, "oidc_login_succeeded",
                  actor_subject_id=principal.subject_id)
        # Lazy org sync on login (idempotent upsert of user / groups / memberships). A failure here
        # must not break the login that just succeeded.
        try:
            database.sync_org_on_login(principal)
        except Exception as exc:  # noqa: BLE001 - 同步失败不影响登录
            log_event(LOGGER, logging.WARNING, "org_sync_failed",
                      actor_subject_id=principal.subject_id, error=str(exc)[:128])
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
    async def auth_guest():
        if config.auth_mode != "local" or not config.guest_login_enabled:
            raise HTTPException(status_code=403, detail="游客登录未启用")
        response = JSONResponse({"ok": True, "redirect": "/"})
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

    @application.get("/health/live")
    async def health_live():
        return {"ok": True, "status": "live"}

    @application.get("/health/ready")
    async def health_ready():
        database_ok, database_reason = database.healthcheck()
        model_ok, model_reason = models.healthcheck()
        embedding_ok, embedding_reason = retriever.healthcheck()
        auth_ok, auth_reason = authenticator.healthcheck()
        knowledge_ok = config.knowledge_path.is_file()
        web_ok = config.web_index_path.is_file()
        checks = {
            "database": {"ok": database_ok, "reason": database_reason},
            "knowledge": {"ok": knowledge_ok, "reason": "ok" if knowledge_ok else "knowledge_missing"},
            "web": {"ok": web_ok, "reason": "ok" if web_ok else "web_index_missing"},
            "model": {"ok": model_ok, "reason": model_reason},
            "embedding": {"ok": embedding_ok, "reason": embedding_reason},
            "authentication": {"ok": auth_ok, "reason": auth_reason},
        }
        ready = all(item["ok"] for item in checks.values())
        return JSONResponse(
            {"ok": ready, "status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    @application.post("/api/query")
    def query(req: QueryReq, principal: Principal = Depends(viewer)):
        # Per-user daily quota. Skipped in development mode (shared dev identity) and for
        # anonymous/empty subjects. Anonymous skipping keeps the local offline demo usable.
        ratelimit_headers: dict[str, str] = {}
        if config.auth_mode != "development" and rate_limiter.enabled and principal.subject_id:
            allowed, remaining, retry_after = rate_limiter.hit(principal.subject_id)
            ratelimit_headers = {
                "X-RateLimit-Limit": str(config.query_daily_quota),
                "X-RateLimit-Remaining": str(remaining),
            }
            if not allowed:
                return JSONResponse(
                    {"ok": False, "error": "今日查询次数已达上限", "retry_after_seconds": retry_after},
                    status_code=429,
                    headers={"Retry-After": str(retry_after), **ratelimit_headers},
                )
        try:
            result = service.query(req.session_id, req.question, principal)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except ModelGatewayError:
            return JSONResponse({"ok": False, "error": "模型服务暂时不可用，请稍后重试"}, status_code=502)
        # Non-fatal persistence of procurement-gap signals (references + knowledge gaps). A failure
        # here must never block the answer the user already received (cf. record_model_attempts).
        try:
            database.record_citations(result["query_id"], result.get("citations") or [])
            if result.get("evidence") == "insufficient":
                database.register_knowledge_gap(
                    result["query_id"], "insufficient_evidence",
                    str((result.get("model") or {}).get("route", "")),
                )
        except Exception as exc:  # noqa: BLE001 - 落库失败不阻断答案
            log_event(
                LOGGER, logging.WARNING, "procurement_signal_persist_failed",
                query_id=result.get("query_id"), error=str(exc)[:128],
            )
        return JSONResponse({"ok": True, **result}, headers=ratelimit_headers)

    @application.post("/api/feedback")
    def submit_feedback(req: FeedbackReq, principal: Principal = Depends(viewer)):
        """Persist a user's rating for a query. Idempotent; not self-audited (matches /api/query)."""
        if req.rating not in FEEDBACK_RATING:
            return JSONResponse(
                {"ok": False, "error": "反馈评分必须是 positive 或 negative"}, status_code=400,
            )
        comment = (req.comment or "")[:1000]
        try:
            database.record_feedback(req.query_id, principal.subject_id, req.rating, comment)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    @application.get("/api/history")
    async def history(session_id: str = "default", limit: int = 20,
                      principal: Principal = Depends(viewer)):
        return {"ok": True, "items": database.history(
            session_id, limit, owner_subject_id=principal.subject_id,
        )}

    @application.get("/api/me")
    async def me(principal: Principal = Depends(authenticated)):
        return {
            "ok": True,
            "subject_id": principal.subject_id,
            "display_name": principal.display_name,
            "roles": sorted(principal.roles),
            "groups": sorted(principal.groups),
        }

    @application.get("/api/runtime/model")
    async def model_status(_principal: Principal = Depends(viewer)):
        return {"ok": True, **models.status()}

    @application.get("/api/usage/summary")
    async def usage_summary(session_id: str = "default",
                            principal: Principal = Depends(viewer)):
        return {"ok": True, **database.usage_summary(
            session_id, owner_subject_id=principal.subject_id,
        )}

    @application.get("/api/usage/ledger")
    async def usage_ledger(session_id: str = "default", limit: int = 50,
                           principal: Principal = Depends(auditor)):
        return {"ok": True, "items": database.usage_ledger(
            session_id, limit, owner_subject_id=principal.subject_id,
        )}

    return application


app = create_app()
