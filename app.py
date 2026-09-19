from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from assistant import ITQueryService
from backend import (
    AppSettings, ModelRouter, QueryDatabase, configure_logging, log_event,
    request_id_context,
)


LOGGER = logging.getLogger("docmind.it")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class QueryReq(BaseModel):
    session_id: str = "default"
    question: str = ""


def create_app(settings: AppSettings | None = None) -> FastAPI:
    config = settings or AppSettings.from_environment()
    configure_logging(config)
    database = QueryDatabase(
        config.database_url,
        pool_size=config.database_pool_size,
        max_overflow=config.database_max_overflow,
        pool_timeout=config.database_pool_timeout,
        connect_timeout=config.database_connect_timeout,
    )
    models = ModelRouter(
        config.model_mode,
        local_provider=config.local_provider,
        local_model=config.local_model,
        cloud_provider=config.cloud_provider,
        cloud_model=config.cloud_model,
        builtin_model=config.builtin_model,
        configured_credentials=config.configured_credentials,
    )
    service = ITQueryService(str(config.knowledge_path), database, models)

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
    application.state.service = service

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
            return response
        finally:
            request_id_context.reset(context_token)

    @application.get("/")
    async def index():
        return FileResponse(config.web_index_path)

    @application.get("/health/live")
    async def health_live():
        return {"ok": True, "status": "live"}

    @application.get("/health/ready")
    async def health_ready():
        database_ok, database_reason = database.healthcheck()
        model_ok, model_reason = models.healthcheck()
        knowledge_ok = config.knowledge_path.is_file()
        web_ok = config.web_index_path.is_file()
        checks = {
            "database": {"ok": database_ok, "reason": database_reason},
            "knowledge": {"ok": knowledge_ok, "reason": "ok" if knowledge_ok else "knowledge_missing"},
            "web": {"ok": web_ok, "reason": "ok" if web_ok else "web_index_missing"},
            "model": {"ok": model_ok, "reason": model_reason},
        }
        ready = all(item["ok"] for item in checks.values())
        return JSONResponse(
            {"ok": ready, "status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    @application.post("/api/query")
    async def query(req: QueryReq):
        try:
            return {"ok": True, **service.query(req.session_id, req.question)}
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @application.get("/api/history")
    async def history(session_id: str = "default", limit: int = 20):
        return {"ok": True, "items": database.history(session_id, limit)}

    @application.get("/api/runtime/model")
    async def model_status():
        return {"ok": True, **models.status()}

    return application


app = create_app()
